"""
`RedisEventStore`: the event store shared by every process that reaches one Redis. The only module
of `fastapi_viewsets.events` that uses `redis`, imported lazily by `PerLoopRedis`.
"""

import asyncio
import json
import logging
import threading

from typing import Any, TYPE_CHECKING

from ..conf import settings
from . import store as _store
from .delivery import deliver
from .store import LoggedEvent

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# KEYS: rev counter, log. ARGV: now (ms), event JSON {name, payload, version}, log size, log expiry (ms).
# Sets rev = max(counter + 1, now) and pushes {rev, prev, event} to the head of the trimmed log.
# Returns {rev, prev} as decimal strings, or nil without changes when rev would reach 2^53.
_APPEND_SCRIPT = """
local prev = tonumber(redis.call('GET', KEYS[1]) or '0')
local rev = math.max(prev + 1, tonumber(ARGV[1]))
if rev >= 9007199254740992 then
    return false
end
local rev_text = string.format('%.0f', rev)
local prev_text = string.format('%.0f', prev)
redis.call('SET', KEYS[1], rev_text)
local size = tonumber(ARGV[3])
if size > 0 then
    redis.call('LPUSH', KEYS[2], '{"rev":' .. rev_text .. ',"prev":' .. prev_text .. ',"event":' .. ARGV[2] .. '}')
    redis.call('LTRIM', KEYS[2], 0, size - 1)
    redis.call('PEXPIRE', KEYS[2], ARGV[4])
else
    redis.call('DEL', KEYS[2])
end
return {rev_text, prev_text}
"""

# Seconds the reader waits for a message before it checks again, and waits after a failed read.
DEFAULT_READ_TIMEOUT = 1.0

# Seconds `start` waits for Redis to confirm the SUBSCRIBE of the events channel.
DEFAULT_SUBSCRIBE_TIMEOUT = 5.0

# `Redis.from_url` options `PerLoopRedis` sets unless given (seconds).
DEFAULT_CLIENT_OPTIONS: dict[str, Any] = {"socket_connect_timeout": 2.0, "socket_timeout": 5.0}


class PerLoopRedis:
    """
    Proxies a `redis.asyncio.Redis`, with one client per running event loop (a connection belongs to
    its loop; Celery executions and `async_to_sync()` each run their own). A loop's client is closed when
    the loop cancels its tasks at the end (`asyncio.run()`); otherwise it is dropped by the next client
    creation and closed on garbage collection.
    """

    def __init__(self, url: str, **options: Any):
        from redis.asyncio import Redis

        self.url = url
        self.options = DEFAULT_CLIENT_OPTIONS | options
        self._redis_class = Redis
        self._clients: dict[asyncio.AbstractEventLoop, Redis] = {}
        self._lock = threading.Lock()

    def client(self) -> "Redis":
        """The client of the running loop. Raises `RuntimeError` outside a running loop."""
        loop = asyncio.get_running_loop()
        with self._lock:
            client = self._clients.get(loop)
            if client is not None:
                return client
            for closed in [held for held in self._clients if held.is_closed()]:
                del self._clients[closed]
            client = self._clients[loop] = self._redis_class.from_url(self.url, **self.options)
        closer = loop.create_task(self._close_with_loop(loop, client), name="PerLoopRedis: close with the loop")
        # A loop that ends without cancelling its tasks destroys this one without a warning.
        closer._log_destroy_pending = False  # type: ignore[attr-defined]
        return client

    async def _close_with_loop(self, loop: asyncio.AbstractEventLoop, client: "Redis") -> None:
        # Not on GC finalisation: that can run inside `client()` while it holds `_lock` and would
        # deadlock; such a loop's client is dropped by the next `client()` call.
        try:
            await loop.create_future()
        except asyncio.CancelledError:
            self._forget(loop, client)
            await client.aclose()
            raise

    def _forget(self, loop: asyncio.AbstractEventLoop, client: "Redis") -> None:
        with self._lock:
            if self._clients.get(loop) is client:
                del self._clients[loop]

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.client(), name)


class RedisEventStore:
    """
    An event store in Redis under `key_prefix`: `rev:{topic}`, `log:{topic}` and the `events` channel.
    `start()` subscribes the channel and runs a reader delivering each event to this process's
    subscriptions; `start()` and `stop()` run on the reader's loop. Events published while the pub/sub
    connection is down are missed here; clients recover them by polling.
    """

    def __init__(
        self,
        redis_client: "Redis | PerLoopRedis",
        key_prefix: str = "events:",
        *,
        subscribe_timeout: float = DEFAULT_SUBSCRIBE_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ):
        self.redis_client = redis_client
        self.key_prefix = key_prefix
        self.subscribe_timeout = subscribe_timeout
        self.read_timeout = read_timeout
        self._pubsub: Any = None
        self._reader: asyncio.Task | None = None

    @classmethod
    def from_url(
        cls,
        url: str,
        key_prefix: str = "events:",
        *,
        subscribe_timeout: float = DEFAULT_SUBSCRIBE_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        **options: Any,
    ) -> "RedisEventStore":
        """A store on `PerLoopRedis(url, **options)`, one client per event loop."""
        return cls(
            PerLoopRedis(url, **options),
            key_prefix,
            subscribe_timeout=subscribe_timeout,
            read_timeout=read_timeout,
        )

    def rev_key(self, topic: str) -> str:
        return f"{self.key_prefix}rev:{topic}"

    def log_key(self, topic: str) -> str:
        return f"{self.key_prefix}log:{topic}"

    @property
    def channel(self) -> str:
        """The channel every event is published on."""
        return f"{self.key_prefix}events"

    @property
    def reader_running(self) -> bool:
        """Whether the reader task runs."""
        return self._reader is not None and not self._reader.done()

    async def start(self) -> None:
        """
        Subscribes the events channel and starts the reader once Redis confirms. Raises
        `asyncio.TimeoutError` after `subscribe_timeout` seconds, or the connection's error.
        """
        if self.reader_running:
            return
        pubsub = self.redis_client.pubsub()
        try:
            await pubsub.subscribe(self.channel)
            await asyncio.wait_for(self._confirmed(pubsub), self.subscribe_timeout)
        except BaseException:
            await _close(pubsub)
            raise
        self._pubsub = pubsub
        self._reader = asyncio.create_task(self._read())

    async def _confirmed(self, pubsub: Any) -> None:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=False, timeout=self.read_timeout)
            if message is not None and message.get("type") == "subscribe":
                return

    async def stop(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
        pubsub, self._pubsub = self._pubsub, None
        if pubsub is not None:
            await _close(pubsub)

    async def append(self, topic: str, name: str, payload: Any, version: int) -> tuple[int, int]:
        result = await self.redis_client.eval(
            _APPEND_SCRIPT,
            2,
            self.rev_key(topic),
            self.log_key(topic),
            _store._now_ms(),
            json.dumps({"name": name, "payload": payload, "version": version}),
            max(settings.viewsets_event_log_size, 0),
            max(1, int(settings.viewsets_event_log_ttl * 1000)),
        )
        if result is None:
            raise OverflowError(
                f"the rev of topic {topic} reaches 2**53, which a JSON number on the frontend cannot hold"
            )
        rev, prev = result
        return int(rev), int(prev)

    async def revs(self, topics: list[str]) -> dict[str, int]:
        if not topics:
            return {}
        values = await self.redis_client.mget([self.rev_key(topic) for topic in topics])
        return {topic: int(value) if value is not None else 0 for topic, value in zip(topics, values, strict=True)}

    async def since(self, topic: str, after: int) -> list[LoggedEvent] | None:
        # the rev is read before the log, so the log holds every event up to that rev
        value = await self.redis_client.get(self.rev_key(topic))
        rev = int(value) if value is not None else 0
        if after == rev:
            return []
        chain: list[LoggedEvent] = []
        for raw in await self.redis_client.lrange(self.log_key(topic), 0, -1):
            entry = json.loads(raw)
            event = entry["event"]
            chain.append(LoggedEvent(entry["rev"], entry["prev"], event["name"], event["payload"], event["version"]))
            if entry["prev"] == after:
                chain.reverse()
                return chain
        return None

    async def publish(self, topic: str, rev: int, prev: int, name: str, payload: Any, version: int) -> None:
        message = json.dumps(
            {"topic": topic, "rev": rev, "prev": prev, "name": name, "payload": payload, "version": version}
        )
        await self.redis_client.publish(self.channel, message)

    async def drop(self, topic: str) -> None:
        await self.redis_client.delete(self.rev_key(topic), self.log_key(topic))

    async def _read(self) -> None:
        while True:
            try:
                message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=self.read_timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("events: reading the events channel failed")
                await asyncio.sleep(self.read_timeout)
                continue
            if message is None or message.get("type") != "message":
                continue
            try:
                data = json.loads(message["data"])
                await deliver(data["topic"], data["rev"], data["prev"], data["name"], data["payload"], data["version"])
            except Exception:
                logger.exception("events: an event on %s could not be delivered", message.get("channel"))


async def _close(pubsub: Any) -> None:
    try:
        await pubsub.aclose()
    except Exception:
        logger.debug("events: closing the reader connection failed", exc_info=True)
