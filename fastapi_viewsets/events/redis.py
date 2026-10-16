"""
`RedisEventStore`: the event store shared by every process that reaches one Redis.

The only module of `fastapi_viewsets.events` that uses `redis`. It takes a `redis.asyncio.Redis`
client the application constructs, and imports the `redis` package for type checking only.
"""

import asyncio
import json
import logging

from collections import deque
from typing import Any, TYPE_CHECKING

from ..conf import settings
from . import store as _store
from .delivery import deliver
from .store import LoggedEvent

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# KEYS[1] the topic's rev counter, KEYS[2] the topic's log. ARGV[1] now in milliseconds, ARGV[2] the
# event as the JSON object {"name": ..., "payload": ...}, ARGV[3] the log size, ARGV[4] the log's
# expiry in milliseconds. Sets the counter, without expiry, to max(counter + 1, now) and pushes
# {"rev": ..., "prev": ..., "event": ARGV[2]} to the head of the log, which keeps ARGV[3] entries.
# Returns {rev, prev} as decimal strings, or nil, changing nothing, when rev would reach 2^53.
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
_READ_TIMEOUT = 1.0

# Seconds `watch` waits for Redis to confirm a SUBSCRIBE.
_SUBSCRIBE_TIMEOUT = 5.0


class RedisEventStore:
    """
    An event store in Redis, under `key_prefix`:

    - `{prefix}rev:{topic}`, the topic's rev, a string without expiry;
    - `{prefix}log:{topic}`, the topic's log, a list of JSON entries, newest first, of at most
      `settings.viewsets_event_log_size` entries, expiring `settings.viewsets_event_log_ttl` seconds
      after the topic's last event;
    - `{prefix}ch:{topic}`, the channel `publish` publishes the event on, as the JSON object
      `{"topic", "rev", "prev", "name", "payload"}`.

    `append` is one Lua script. `start()` opens the reader, a task on one pub/sub connection that
    subscribes to the channel of every topic `watch` names and calls `deliver()` for each message;
    `stop()` closes it. A topic watched before `start()` is subscribed when the reader opens.

    While the reader runs, `watch` returns once the reader has read Redis's confirmation of the
    SUBSCRIBE, so every event published on the channel after `watch` returns reaches this process.
    """

    def __init__(self, redis_client: "Redis", key_prefix: str = "events:"):
        self.redis_client = redis_client
        self.key_prefix = key_prefix
        self._watched: set[str] = set()
        self._listening: set[str] = set()
        self._pubsub: Any = None
        self._reader: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        # channel -> one future per SUBSCRIBE sent and not yet confirmed, oldest first
        self._confirming: dict[str, deque[asyncio.Future]] = {}

    def rev_key(self, topic: str) -> str:
        return f"{self.key_prefix}rev:{topic}"

    def log_key(self, topic: str) -> str:
        return f"{self.key_prefix}log:{topic}"

    def channel(self, topic: str) -> str:
        return f"{self.key_prefix}ch:{topic}"

    @property
    def reader_running(self) -> bool:
        """Whether the reader task runs."""
        return self._reader is not None and not self._reader.done()

    async def start(self) -> None:
        if self.reader_running:
            return
        self._pubsub = self.redis_client.pubsub()
        self._wake = asyncio.Event()
        for topic in sorted(self._watched):
            await self._pubsub.subscribe(self.channel(topic))
            self._listening.add(topic)
        self._reader = asyncio.create_task(self._read())

    async def stop(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
        pubsub, self._pubsub = self._pubsub, None
        self._listening.clear()
        for pending in self._confirming.values():
            for confirmed in pending:
                if not confirmed.done():
                    confirmed.set_exception(
                        ConnectionError("the event reader stopped before Redis confirmed SUBSCRIBE")
                    )
                    confirmed.exception()
        self._confirming.clear()
        if pubsub is not None:
            try:
                await pubsub.aclose()
            except Exception:
                logger.debug("events: closing the reader connection failed", exc_info=True)

    async def append(self, topic: str, name: str, payload: Any) -> tuple[int, int]:
        result = await self.redis_client.eval(
            _APPEND_SCRIPT,
            2,
            self.rev_key(topic),
            self.log_key(topic),
            _store._now_ms(),
            json.dumps({"name": name, "payload": payload}),
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
            chain.append(LoggedEvent(entry["rev"], entry["prev"], event["name"], event["payload"]))
            if entry["prev"] == after:
                chain.reverse()
                return chain
        return None

    async def publish(self, topic: str, rev: int, prev: int, name: str, payload: Any) -> None:
        message = json.dumps({"topic": topic, "rev": rev, "prev": prev, "name": name, "payload": payload})
        await self.redis_client.publish(self.channel(topic), message)

    async def drop(self, topic: str) -> None:
        await self.redis_client.delete(self.rev_key(topic), self.log_key(topic))

    async def watch(self, topic: str) -> None:
        """
        Records `topic` for `start()`. While the reader runs, SUBSCRIBEs its channel, unless it is
        subscribed, and returns once the reader has read the confirmation of that SUBSCRIBE, or of
        the one still unconfirmed. Raises `asyncio.TimeoutError` when no confirmation arrives within
        5 seconds, and `ConnectionError` when the reader stops first.
        """
        self._watched.add(topic)
        if self._pubsub is None:
            return
        channel = self.channel(topic)
        if topic in self._listening:
            pending = self._confirming.get(channel)
            if not pending:
                return
            confirmed = pending[-1]
        else:
            confirmed = asyncio.get_running_loop().create_future()
            pending = self._confirming.setdefault(channel, deque())
            pending.append(confirmed)
            try:
                await self._pubsub.subscribe(channel)
            except BaseException:
                pending.remove(confirmed)
                if not pending and self._confirming.get(channel) is pending:
                    del self._confirming[channel]
                raise
            self._listening.add(topic)
            self._wake.set()
        await asyncio.wait_for(asyncio.shield(confirmed), _SUBSCRIBE_TIMEOUT)

    def _confirm(self, channel: Any) -> None:
        """Completes the oldest unconfirmed SUBSCRIBE of `channel`."""
        if isinstance(channel, bytes):
            channel = channel.decode()
        pending = self._confirming.get(channel)
        if not pending:
            return
        confirmed = pending.popleft()
        if not pending:
            del self._confirming[channel]
        if not confirmed.done():
            confirmed.set_result(None)

    async def unwatch(self, topic: str) -> None:
        self._watched.discard(topic)
        if self._pubsub is not None and topic in self._listening:
            self._listening.discard(topic)
            await self._pubsub.unsubscribe(self.channel(topic))

    async def _read(self) -> None:
        while True:
            if not self._listening:
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                message = await self._pubsub.get_message(ignore_subscribe_messages=False, timeout=_READ_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("events: reading the event channels failed")
                await asyncio.sleep(_READ_TIMEOUT)
                continue
            if message is None:
                continue
            if message.get("type") == "subscribe":
                self._confirm(message.get("channel"))
                continue
            if message.get("type") != "message":
                continue
            try:
                data = json.loads(message["data"])
                await deliver(data["topic"], data["rev"], data["prev"], data["name"], data["payload"])
            except Exception:
                logger.exception("events: a message on %s could not be delivered", message.get("channel"))
