"""Fakes and the per-test reset shared by the tests of `fastapi_viewsets.events`."""

import asyncio
import time

from collections.abc import Callable
from typing import Any

import pytest

from ..conf import settings
from ..error_namespace import ERROR_NAMESPACE
from . import store as store_module
from .delivery import reset_subscriptions
from .redis import _APPEND_SCRIPT
from .scopes import reset_event_scopes
from .sources import reset_sources

EVENT_SETTINGS = (
    "viewsets_event_store",
    "viewsets_event_authorizers",
    "viewsets_event_delivery_filters",
    "viewsets_event_listeners",
    "viewsets_event_log_size",
    "viewsets_event_log_ttl",
    "viewsets_event_push_timeout",
    "viewsets_event_max_subscriptions",
    "viewsets_event_poll_after_ms",
    "viewsets_event_reauthorize_after",
)


@pytest.fixture(autouse=True)
def _reset_events():
    saved = {name: getattr(settings, name) for name in EVENT_SETTINGS}
    settings.viewsets_event_authorizers = []
    settings.viewsets_event_delivery_filters = []
    settings.viewsets_event_listeners = []
    reset_subscriptions()
    reset_sources()
    reset_event_scopes()
    store_module._default_store = None
    yield
    for name, value in saved.items():
        setattr(settings, name, value)
    reset_subscriptions()
    reset_sources()
    reset_event_scopes()
    store_module._default_store = None


class ConnectionLost(Exception):  # noqa: N818
    """Has the name of muxws's `ConnectionLost`, which is how the events package recognises it."""


class FakePeer:
    """
    The part of a muxws acceptor peer the events package uses, as muxws behaves: `notify()` queues
    the push without awaiting anything and raises `ConnectionLost` on a closed peer; `close()` sets
    `is_open` false and runs the `on_close` handlers registered until then, once.
    """

    def __init__(self, *, error: BaseException | None = None):
        self.error = error
        self.is_open = True
        self.pushes: list[tuple[Any, dict[str, Any]]] = []
        self.close_handlers: list = []

    async def notify(self, payload: Any = None, *, headers: dict[str, Any] | None = None) -> None:
        if not self.is_open:
            raise ConnectionLost("the peer is closed")
        if self.error is not None:
            raise self.error
        self.pushes.append((payload, headers))

    def on_close(self, handler):
        self.close_handlers.append(handler)
        return handler

    def close(self) -> None:
        if not self.is_open:
            return
        self.is_open = False
        for handler in list(self.close_handlers):
            handler(None)


class FakeRedis:
    """
    The subset of `redis.asyncio.Redis` `RedisEventStore` uses, without `decode_responses`: strings,
    lists, key expiry, pub/sub, and `eval` of the append script, emulated.
    """

    def __init__(self):
        self.strings: dict[str, bytes] = {}
        self.lists: dict[str, list[bytes]] = {}
        self.expires: dict[str, float] = {}
        self.channels: dict[str, list[FakePubSub]] = {}
        self.commands: list[str] = []
        self.pubsub_held = False
        self.opened: list[FakePubSub] = []

    def _expire(self, key: str) -> None:
        if key in self.expires and self.expires[key] <= time.monotonic():
            del self.expires[key]
            self.strings.pop(key, None)
            self.lists.pop(key, None)

    async def get(self, key: str) -> bytes | None:
        self.commands.append("GET")
        self._expire(key)
        return self.strings.get(key)

    async def mget(self, keys: list[str]) -> list[bytes | None]:
        self.commands.append("MGET")
        for key in keys:
            self._expire(key)
        return [self.strings.get(key) for key in keys]

    async def delete(self, *keys: str) -> int:
        self.commands.append("DEL")
        deleted = 0
        for key in keys:
            self._expire(key)
            deleted += (self.strings.pop(key, None) is not None) + (self.lists.pop(key, None) is not None)
            self.expires.pop(key, None)
        return deleted

    async def lrange(self, key: str, start: int, end: int) -> list[bytes]:
        self.commands.append("LRANGE")
        self._expire(key)
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start : end + 1]

    async def pttl(self, key: str) -> int:
        self._expire(key)
        if key not in self.strings and key not in self.lists:
            return -2
        if key not in self.expires:
            return -1
        return int((self.expires[key] - time.monotonic()) * 1000)

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any):
        self.commands.append("EVAL")
        if script != _APPEND_SCRIPT or numkeys != 2:
            raise NotImplementedError("FakeRedis evaluates the append script of RedisEventStore only")
        rev_key, log_key, now_ms, event, size, ttl_ms = keys_and_args
        self._expire(rev_key)
        self._expire(log_key)
        prev = int(self.strings.get(rev_key, b"0"))
        rev = max(prev + 1, int(now_ms))
        if rev >= 2**53:
            return None
        self.strings[rev_key] = str(rev).encode()
        if int(size) > 0:
            log = self.lists.setdefault(log_key, [])
            log.insert(0, f'{{"rev":{rev},"prev":{prev},"event":{event}}}'.encode())
            del log[int(size) :]
            self.expires[log_key] = time.monotonic() + int(ttl_ms) / 1000
        else:
            self.lists.pop(log_key, None)
            self.expires.pop(log_key, None)
        return [str(rev).encode(), str(prev).encode()]

    async def publish(self, channel: str, message: str) -> int:
        self.commands.append("PUBLISH")
        subscribers = list(self.channels.get(channel, []))
        for pubsub in subscribers:
            pubsub.queue.put_nowait({"type": "message", "channel": channel.encode(), "data": message.encode()})
        return len(subscribers)

    def pubsub(self) -> "FakePubSub":
        """A new pub/sub connection, `held` while `pubsub_held` is set; kept in `opened`."""
        pubsub = FakePubSub(self)
        pubsub.held = self.pubsub_held
        self.opened.append(pubsub)
        return pubsub


class FakePubSub:
    """
    A pub/sub connection, as `redis.asyncio.client.PubSub` without `decode_responses`.

    `subscribe` and `unsubscribe` send the command and return, as redis-py's do; Redis applies it
    and queues its confirmation at once, or, while `held` is set, when `release()` is called, in the
    order the commands were sent. `channels` holds the channels the client has SUBSCRIBEd and not
    UNSUBSCRIBEd. `lose_connection()` reconnects as redis-py does.
    """

    def __init__(self, redis: FakeRedis):
        self.redis = redis
        self.queue: asyncio.Queue = asyncio.Queue()
        self.subscribed: set[str] = set()
        self.channels: set[str] = set()
        self.closed = False
        self.held = False
        self._unapplied: list[tuple[Callable[[str], None], str]] = []

    async def subscribe(self, *channels: str) -> None:
        for channel in channels:
            self.channels.add(channel)
            self._send(self._apply_subscribe, channel)

    async def unsubscribe(self, *channels: str) -> None:
        for channel in channels:
            self.channels.discard(channel)
            self._send(self._apply_unsubscribe, channel)

    def _send(self, apply: Callable[[str], None], channel: str) -> None:
        if self.held or self._unapplied:
            self._unapplied.append((apply, channel))
        else:
            apply(channel)

    def release(self) -> None:
        """Applies and confirms the commands sent while `held` was set."""
        self.held = False
        unapplied, self._unapplied = self._unapplied, []
        for apply, channel in unapplied:
            apply(channel)

    def lose_connection(self) -> None:
        """
        Drops the connection with every command Redis has not applied and every reply not read, and
        reconnects as redis-py does: SUBSCRIBEs every channel in `channels`. Clears `held`.
        """
        for channel in self.subscribed:
            self.redis.channels[channel].remove(self)
        self.subscribed.clear()
        self._unapplied.clear()
        while not self.queue.empty():
            self.queue.get_nowait()
        self.held = False
        for channel in sorted(self.channels):
            self._apply_subscribe(channel)

    def _apply_subscribe(self, channel: str) -> None:
        if channel not in self.subscribed:
            self.subscribed.add(channel)
            self.redis.channels.setdefault(channel, []).append(self)
        self.queue.put_nowait({"type": "subscribe", "channel": channel.encode(), "data": len(self.subscribed)})

    def _apply_unsubscribe(self, channel: str) -> None:
        if channel in self.subscribed:
            self.subscribed.discard(channel)
            self.redis.channels[channel].remove(self)
        self.queue.put_nowait({"type": "unsubscribe", "channel": channel.encode(), "data": len(self.subscribed)})

    async def get_message(self, ignore_subscribe_messages: bool = False, timeout: float = 0.0):
        try:
            message = await asyncio.wait_for(self.queue.get(), timeout)
        except asyncio.TimeoutError:
            return None
        if ignore_subscribe_messages and message["type"] != "message":
            return None
        return message

    async def aclose(self) -> None:
        await self.unsubscribe(*list(self.subscribed))
        self.closed = True


def envelope(detail: str, code: str, **params: Any) -> dict[str, Any]:
    """The `ApiErrorBody` of the package's error `code` (without the namespace)."""
    return {"detail": detail, "detail_code": f"{ERROR_NAMESPACE}.{code}", "detail_params": params, "field_errors": {}}
