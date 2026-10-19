"""
The subscriptions of this process and the delivery of an event to them.

A subscription is one (socket, client topic) pair, held by the process that holds the socket. Its
events are delivered by its store topic: the client topic itself, or, for a scoped viewset topic,
the `@{name}:{key}` the server resolved for the subscriber (see `fastapi_viewsets.events.scopes`).
A socket is the muxws peer the request scope carries (`routing.PEER_SCOPE_KEY`); this module calls
its `notify()` and `on_close()` and imports nothing of muxws.
"""

import asyncio
import dataclasses
import inspect
import logging
import time

from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from starlette.exceptions import HTTPException

from ..conf import settings
from ..context import Context
from ..exceptions import EventSubscriptionsExceededError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, eq=False)
class Subscription:
    """
    One socket following one topic, or, for the delivery filters of a poll, the poll of one topic.
    """

    peer: Any | None
    """The muxws peer of the socket. On a poll, the peer the poll arrived on; None for a poll over
    REST."""
    context: Context
    """The context of the `subscribe` call that made the subscription; on a poll, the context of the
    poll call."""
    topic: str
    """The client topic: what the socket subscribed to and receives in `:topic`."""
    store_topic: str | None = None
    """The topic the events are delivered under; None is `topic`."""
    scope: str | None = None
    """The scope key as text, for a scoped viewset topic; None for an unscoped topic."""
    authorized_at: float = 0.0
    """`time.monotonic()` of the subscribe, or of the last reauthorization."""
    reauthorize: Callable[[], Awaitable[str]] | None = field(default=None, repr=False)
    """Runs the topic's gate again and returns the store topic it answers; raises `HTTPException`
    on a refusal. None for a subscription that is never reauthorized."""


DeliveryFilter = Callable[[Subscription, str, int, str, Any, int], "bool | Awaitable[bool]"]
"""
`filter(subscription, topic, rev, event_name, payload, version)`, sync or async; `version` is the
event's `event_version`. The filters run on every push and on every event a poll answers. False
skips the push to that subscription's socket, or leaves the event out of the poll answer; an
exception counts as True. On a poll, `subscription` is built from the poll call and its `peer` is
None for a poll over REST. `topic` is the client topic; `subscription.store_topic` and
`subscription.scope` name the scope of a scoped topic.
"""

EventListener = Callable[[str, int, int, str, Any, int], "None | Awaitable[None]"]
"""
`listener(topic, rev, prev, event_name, payload, version)`, sync or async; `topic` is the store
topic. `deliver()` calls the listeners with every event, in every process whose store is started,
whether or not the process holds a subscription of the topic, before it pushes the event; a listener
that removes subscriptions (`drop_subscriptions`) removes them before the push.
"""

# store topic -> (id(peer), client topic) -> Subscription
_by_topic: dict[str, dict[tuple[int, str], Subscription]] = {}
# id(peer) -> client topic -> Subscription
_by_peer: dict[int, dict[str, Subscription]] = {}
# (id(peer), client topic, id(subscription)) -> the reauthorization running for that subscription
_reauth_tasks: dict[tuple[int, str, int], asyncio.Task] = {}
# id(peer) of every socket whose on_close handler is installed
_close_installed: set[int] = set()
# background tasks: pushes
_tasks: set[asyncio.Task] = set()


def _spawn(coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task | None:
    try:
        task = asyncio.get_running_loop().create_task(coroutine)
    except RuntimeError:
        coroutine.close()
        return None
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def _store_topic_of(subscription: Subscription) -> str:
    return subscription.store_topic if subscription.store_topic is not None else subscription.topic


def _remove(key: int, topic: str, expected: Subscription | None = None) -> Subscription | None:
    """Removes the socket's subscription to the client topic `topic`; with `expected`, only while the
    table holds that very subscription."""
    held = _by_peer.get(key)
    subscription = held.get(topic) if held is not None else None
    if subscription is None or (expected is not None and subscription is not expected):
        return None
    del held[topic]
    if not held:
        del _by_peer[key]
    _drop_from_topic(key, subscription)
    return subscription


def _drop_from_topic(key: int, subscription: Subscription) -> None:
    store_topic = _store_topic_of(subscription)
    subscribers = _by_topic.get(store_topic)
    if subscribers is not None and subscribers.get((key, subscription.topic)) is subscription:
        del subscribers[(key, subscription.topic)]
        if not subscribers:
            del _by_topic[store_topic]


async def subscribe(
    peer: Any,
    context: Context,
    topic: str,
    *,
    store_topic: str | None = None,
    scope: str | None = None,
    reauthorize: Callable[[], Awaitable[str]] | None = None,
) -> Subscription:
    """
    Subscribes the socket `peer` to the client topic `topic` and returns the subscription. From then
    on `deliver()` pushes the events of `store_topic` (None: `topic`) to the socket. A topic the
    socket already holds is subscribed again with the new arguments, replacing the old subscription,
    and counts once. `scope` is the scope key of a scoped topic; `reauthorize` is the function a
    push calls once `settings.viewsets_event_reauthorize_after` has passed (see `Subscription`).

    Raises `EventSubscriptionsExceededError` (409) when the socket already holds
    `settings.viewsets_event_max_subscriptions` other topics. The first subscription of a socket
    installs one `peer.on_close` handler, which drops every subscription of the socket.

    A socket whose `is_open` is false is closed, and its `on_close` handlers have run: the
    subscription is returned without entering the table.
    """
    subscription = Subscription(
        peer,
        context,
        topic,
        store_topic=store_topic,
        scope=scope,
        authorized_at=time.monotonic(),
        reauthorize=reauthorize,
    )
    if not getattr(peer, "is_open", True):
        return subscription
    key = id(peer)
    held = _by_peer.get(key, {})
    limit = settings.viewsets_event_max_subscriptions
    if topic not in held and len(held) >= limit:
        raise EventSubscriptionsExceededError(limit)
    previous = held.get(topic)
    if previous is not None:
        _drop_from_topic(key, previous)
    _by_peer.setdefault(key, {})[topic] = subscription
    _by_topic.setdefault(_store_topic_of(subscription), {})[(key, topic)] = subscription
    if key not in _close_installed:
        _close_installed.add(key)
        peer.on_close(lambda _reason: drop_peer(peer))
    return subscription


async def unsubscribe(peer: Any, topic: str) -> None:
    """Removes the socket's subscription to the client topic `topic`, if it holds one."""
    _remove(id(peer), topic)


def drop_subscriptions(where: Callable[[Subscription], bool]) -> int:
    """
    Removes every subscription of this process for which `where(subscription)` is true, and returns
    how many it removed. For a membership revoked at once:
    `drop_subscriptions(lambda s: s.scope == "7" and s.context.raw().get("user_id") == 12)`.
    """
    dropped = 0
    for key, held in list(_by_peer.items()):
        for topic, subscription in list(held.items()):
            if where(subscription) and _remove(key, topic, subscription) is not None:
                dropped += 1
    return dropped


def drop_peer(peer: Any) -> None:
    """Removes every subscription of the socket `peer`."""
    key = id(peer)
    _close_installed.discard(key)
    for topic in list(_by_peer.get(key, {})):
        _remove(key, topic)


def subscriptions(topic: str) -> list[Subscription]:
    """The subscriptions of this process whose events are delivered under the store topic `topic`."""
    return list(_by_topic.get(topic, {}).values())


def topics_of(peer: Any) -> list[str]:
    """The client topics the socket `peer` holds."""
    return list(_by_peer.get(id(peer), {}))


def _is_connection_lost(error: BaseException) -> bool:
    return any(cls.__name__ == "ConnectionLost" for cls in type(error).__mro__)


async def passes_filters(
    subscription: Subscription, topic: str, rev: int, name: str, payload: Any, version: int
) -> bool:
    """
    Runs `settings.viewsets_event_delivery_filters` in order and answers False when one of them
    answers False. A filter that raises counts as True; the exception is logged at debug level.
    """
    for delivery_filter in settings.viewsets_event_delivery_filters:
        try:
            result = delivery_filter(subscription, topic, rev, name, payload, version)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            logger.debug(
                "events: a delivery filter raised for topic %s rev %s; it counts as True", topic, rev, exc_info=True
            )
            continue
        if result is False:
            return False
    return True


def _is_refusal(error: BaseException) -> bool:
    """Whether `error` refuses a topic for good: an `HTTPException` of a status from 400 to 499 other
    than 429. A 429 and a status of 500 or above are transient, as the frontend's event client
    counts them."""
    return isinstance(error, HTTPException) and 400 <= error.status_code < 500 and error.status_code != 429


async def _reauthorized(subscription: Subscription) -> bool:
    """
    Runs `subscription.reauthorize()` once for every push that needs it at the same time. True when
    it answers the subscription's store topic; the subscription's `authorized_at` is renewed. A
    refusal (`_is_refusal`) or another store topic removes the subscription; any other failure,
    a 429 or 5xx `HTTPException` included, keeps it as it is and skips the push.
    """
    key = (id(subscription.peer), subscription.topic, id(subscription))
    task = _reauth_tasks.get(key)
    if task is None:
        task = asyncio.get_running_loop().create_task(_reauthorize(subscription))
        _reauth_tasks[key] = task

        def forget(done: asyncio.Task) -> None:
            if _reauth_tasks.get(key) is done:
                del _reauth_tasks[key]

        task.add_done_callback(forget)
    return await asyncio.shield(task)


async def _reauthorize(subscription: Subscription) -> bool:
    peer_key = id(subscription.peer)
    try:
        store_topic = await subscription.reauthorize()
    except Exception as error:
        if _is_refusal(error):
            _remove(peer_key, subscription.topic, subscription)
            logger.debug("events: the reauthorization of topic %s refused the subscription", subscription.topic)
        else:
            logger.debug(
                "events: the reauthorization of topic %s failed; the push is skipped",
                subscription.topic,
                exc_info=True,
            )
        return False
    if store_topic != _store_topic_of(subscription):
        _remove(peer_key, subscription.topic, subscription)
        logger.debug(
            "events: the reauthorization of topic %s answered another store topic; the subscription is removed",
            subscription.topic,
        )
        return False
    held = _by_peer.get(peer_key)
    if held is not None and held.get(subscription.topic) is subscription:
        renewed = dataclasses.replace(subscription, authorized_at=time.monotonic())
        held[subscription.topic] = renewed
        _by_topic[_store_topic_of(subscription)][(peer_key, subscription.topic)] = renewed
    return True


def _needs_reauthorization(subscription: Subscription) -> bool:
    after = settings.viewsets_event_reauthorize_after
    return (
        subscription.reauthorize is not None
        and after is not None
        and time.monotonic() - subscription.authorized_at >= after
    )


async def _push(
    subscription: Subscription, topic: str, rev: int, prev: int, name: str, payload: Any, version: int
) -> None:
    async def push() -> None:
        if _needs_reauthorization(subscription) and not await _reauthorized(subscription):
            return
        client_topic = subscription.topic
        if await passes_filters(subscription, client_topic, rev, name, payload, version):
            headers = {":event": name, ":version": version, ":topic": client_topic, ":rev": rev, ":prev": prev}
            if subscription.scope is not None:
                headers[":scope"] = subscription.scope
            await subscription.peer.notify(payload, headers=headers)

    try:
        await asyncio.wait_for(push(), settings.viewsets_event_push_timeout)
    except Exception as error:
        if _is_connection_lost(error):
            drop_peer(subscription.peer)
        else:
            logger.debug("events: the push of topic %s rev %s to a socket failed", topic, rev, exc_info=True)


async def deliver(topic: str, rev: int, prev: int, name: str, payload: Any, version: int) -> None:
    """
    Calls `settings.viewsets_event_listeners` in list order with the event, awaiting each; a listener
    that raises is logged and the next one runs. Then pushes the event of the store topic `topic` to
    every subscription delivered under it in this process, each push in a task of its own, and
    returns without waiting for them. A push runs the
    subscription's reauthorization when it is due (see `settings.viewsets_event_reauthorize_after`),
    the delivery filters with the client topic, and then `peer.notify(payload, headers={":event":
    name, ":version": version, ":topic": subscription.topic, ":rev": rev, ":prev": prev})`, with
    `":scope": subscription.scope` added for a scoped topic, together bounded by
    `settings.viewsets_event_push_timeout` seconds. A push that raises a `ConnectionLost` drops
    every subscription of its socket; any other failure is logged at debug level.
    """
    for listener in settings.viewsets_event_listeners:
        try:
            result = listener(topic, rev, prev, name, payload, version)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("events: a listener raised for topic %s rev %s", topic, rev)
    for subscription in subscriptions(topic):
        _spawn(_push(subscription, topic, rev, prev, name, payload, version))


async def wait_for_pushes() -> None:
    """Waits until the background tasks of this module running now and started meanwhile are done."""
    while True:
        pending = [task for task in _tasks if not task.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def reset_subscriptions() -> None:
    """Forgets every subscription of this process."""
    _by_topic.clear()
    _by_peer.clear()
    _close_installed.clear()
    _reauth_tasks.clear()
