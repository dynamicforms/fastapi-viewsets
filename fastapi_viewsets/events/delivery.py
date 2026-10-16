"""
The subscriptions of this process and the delivery of an event to them.

A subscription is one (socket, topic) pair, held by the process that holds the socket. A socket is
the muxws peer the request scope carries (`routing.PEER_SCOPE_KEY`); this module calls its
`notify()` and `on_close()` and imports nothing of muxws.
"""

import asyncio
import inspect
import logging

from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from ..conf import settings
from ..context import Context
from ..exceptions import EventSubscriptionsExceededError
from .store import get_store

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


DeliveryFilter = Callable[[Subscription, str, int, str, Any, int], "bool | Awaitable[bool]"]
"""
`filter(subscription, topic, rev, event_name, payload, version)`, sync or async; `version` is the
event's `event_version`. The filters run on every push and on every event a poll answers. False
skips the push to that subscription's socket, or leaves the event out of the poll answer; an
exception counts as True. On a poll, `subscription` is built from the poll call and its `peer` is
None for a poll over REST.
"""

# topic -> id(peer) -> Subscription
_by_topic: dict[str, dict[int, Subscription]] = {}
# id(peer) -> topic -> Subscription
_by_peer: dict[int, dict[str, Subscription]] = {}
# id(peer) of every socket whose on_close handler is installed
_close_installed: set[int] = set()
# the topics this process has called store.watch() for and not store.unwatch() since
_watched: set[str] = set()
# topic -> the future that completes when the running call bringing its watch in line with the
# table ends
_syncing: dict[str, asyncio.Future] = {}
# background tasks: pushes, and unwatches after a socket loss
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


async def _sync_watch(topic: str) -> None:
    """
    Calls `store.watch(topic)` when the topic has subscriptions and is not watched, and
    `store.unwatch(topic)` when it has none and is watched, until the two agree, and returns when
    they agree. One call per topic runs the store at a time: a call made while another runs waits
    for it to end, whatever its outcome, and then checks again. An exception of a store call this
    call made propagates. A `store.watch` that raises is followed by `store.unwatch`, whose
    exception is logged, and the topic counts as not watched.
    """
    while (running := _syncing.get(topic)) is not None:
        await asyncio.shield(running)
    done = asyncio.get_running_loop().create_future()
    _syncing[topic] = done
    try:
        while True:
            wanted = topic in _by_topic
            if wanted == (topic in _watched):
                return
            store = get_store()
            if wanted:
                try:
                    await store.watch(topic)
                except BaseException:
                    await _unwatch_failed(store, topic)
                    raise
                _watched.add(topic)
            else:
                _watched.discard(topic)
                await store.unwatch(topic)
    finally:
        if _syncing.get(topic) is done:
            del _syncing[topic]
        done.set_result(None)


async def _unwatch_failed(store: Any, topic: str) -> None:
    """Stops what a failed `store.watch(topic)` may have started; an exception it raises is logged."""
    try:
        await store.unwatch(topic)
    except Exception:
        logger.warning("events: unwatching topic %s after its watch failed raised", topic, exc_info=True)


async def _sync_watch_logged(topic: str) -> None:
    try:
        await _sync_watch(topic)
    except Exception:
        logger.warning(
            "events: bringing the watch of topic %s in line with its subscriptions failed", topic, exc_info=True
        )


def _remove(key: int, topic: str) -> Subscription | None:
    held = _by_peer.get(key)
    subscription = held.pop(topic, None) if held is not None else None
    if held is not None and not held:
        del _by_peer[key]
    subscribers = _by_topic.get(topic)
    if subscribers is not None:
        subscribers.pop(key, None)
        if not subscribers:
            del _by_topic[topic]
    return subscription


async def subscribe(peer: Any, context: Context, topic: str) -> Subscription:
    """
    Subscribes the socket `peer` to `topic` and returns the subscription once the store watches the
    topic. A topic the socket already holds is subscribed again with `context`, and counts once.

    Raises `EventSubscriptionsExceededError` (409) when the socket already holds
    `settings.viewsets_event_max_subscriptions` other topics. The first subscription of a topic in
    this process calls `store.watch(topic)`; when that raises, the subscription is removed and the
    exception propagates. A subscribe that arrives while the topic's watch is being brought in line
    waits for it. The first subscription of a socket installs one `peer.on_close` handler, which
    drops every subscription of the socket.

    A socket whose `is_open` is false is closed, and its `on_close` handlers have run: the
    subscription is returned without entering the table, and the store is not called.
    """
    subscription = Subscription(peer, context, topic)
    if not getattr(peer, "is_open", True):
        return subscription
    key = id(peer)
    held = _by_peer.get(key, {})
    limit = settings.viewsets_event_max_subscriptions
    if topic not in held and len(held) >= limit:
        raise EventSubscriptionsExceededError(limit)
    _by_peer.setdefault(key, {})[topic] = subscription
    _by_topic.setdefault(topic, {})[key] = subscription
    if key not in _close_installed:
        _close_installed.add(key)
        peer.on_close(lambda _reason: drop_peer(peer))
    try:
        await _sync_watch(topic)
    except BaseException:
        if _by_peer.get(key, {}).get(topic) is subscription:
            _remove(key, topic)
        raise
    return subscription


async def unsubscribe(peer: Any, topic: str) -> None:
    """Removes the socket's subscription to `topic`, if it holds one. The last subscription of a
    topic in this process calls `store.unwatch(topic)`; an exception it raises is logged."""
    if _remove(id(peer), topic) is not None and topic not in _by_topic:
        await _sync_watch_logged(topic)


def drop_peer(peer: Any) -> None:
    """Removes every subscription of the socket `peer`. The topics left without subscriptions are
    unwatched in a background task of the running event loop."""
    key = id(peer)
    _close_installed.discard(key)
    for topic in list(_by_peer.get(key, {})):
        _remove(key, topic)
        if topic not in _by_topic:
            _spawn(_sync_watch_logged(topic))


def subscriptions(topic: str) -> list[Subscription]:
    """The subscriptions of `topic` in this process."""
    return list(_by_topic.get(topic, {}).values())


def topics_of(peer: Any) -> list[str]:
    """The topics the socket `peer` holds."""
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


async def _push(
    subscription: Subscription, topic: str, rev: int, prev: int, name: str, payload: Any, version: int
) -> None:
    async def push() -> None:
        if await passes_filters(subscription, topic, rev, name, payload, version):
            headers = {":event": name, ":version": version, ":topic": topic, ":rev": rev, ":prev": prev}
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
    Pushes one event to every subscription of `topic` in this process, each push in a task of its
    own, and returns without waiting for them. A push runs the delivery filters and then
    `peer.notify(payload, headers={":event": name, ":version": version, ":topic": topic, ":rev": rev,
    ":prev": prev})`, together bounded by `settings.viewsets_event_push_timeout` seconds. A push that
    raises a `ConnectionLost` drops every subscription of its socket; any other failure is logged at
    debug level.
    """
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
    """Forgets every subscription and watch of this process without calling the store."""
    _by_topic.clear()
    _by_peer.clear()
    _close_installed.clear()
    _watched.clear()
    _syncing.clear()
