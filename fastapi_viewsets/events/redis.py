"""
`RedisEventStore`: the event store shared by every process that reaches one Redis.

The only module of `fastapi_viewsets.events` that uses `redis`. It takes a `redis.asyncio.Redis`
client the application constructs, and imports the `redis` package for type checking only.
"""

import asyncio
import json
import logging

from typing import Any, TYPE_CHECKING

from ..conf import settings
from . import store as _store
from .delivery import deliver
from .store import LoggedEvent

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# KEYS[1] the topic's rev counter, KEYS[2] the topic's log. ARGV[1] now in milliseconds, ARGV[2] the
# event as the JSON object {"name": ..., "payload": ..., "version": ...}, ARGV[3] the log size,
# ARGV[4] the log's expiry in milliseconds. Sets the counter, without expiry, to max(counter + 1, now)
# and pushes {"rev": ..., "prev": ..., "event": ARGV[2]} to the head of the log, which keeps ARGV[3]
# entries. Returns {rev, prev} as decimal strings, or nil, changing nothing, when rev would reach 2^53.
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

# Seconds `start` waits for Redis to confirm the SUBSCRIBE of the events channel.
_SUBSCRIBE_TIMEOUT = 5.0


class RedisEventStore:
    """
    An event store in Redis, under `key_prefix`:

    - `{prefix}rev:{topic}`, the topic's rev, a string without expiry;
    - `{prefix}log:{topic}`, the topic's log, a list of JSON entries, newest first, of at most
      `settings.viewsets_event_log_size` entries, expiring `settings.viewsets_event_log_ttl` seconds
      after the topic's last event;
    - `{prefix}events`, the one channel `publish` publishes every event on, as the JSON object
      `{"topic", "rev", "prev", "name", "payload", "version"}`.

    `append` is one Lua script. `start()` SUBSCRIBEs the events channel on one pub/sub connection,
    returns once Redis has confirmed it, and runs the reader, a task that calls `deliver()` for each
    message; `deliver()` pushes the event to the subscriptions of its topic in this process and
    leaves out a topic without one. `stop()` closes the connection.

    When redis-py reconnects the pub/sub connection it SUBSCRIBEs the channel again. An event
    published while the connection is down does not reach this process; the event client finds it
    by poll, on the topic's next push or its check poll.
    """

    def __init__(self, redis_client: "Redis", key_prefix: str = "events:"):
        self.redis_client = redis_client
        self.key_prefix = key_prefix
        self._pubsub: Any = None
        self._reader: asyncio.Task | None = None

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
        SUBSCRIBEs the events channel and starts the reader once Redis has confirmed the SUBSCRIBE.
        Raises `asyncio.TimeoutError` when the confirmation does not arrive within 5 seconds, and
        the error of the connection when it fails; the store is then not started.
        """
        if self.reader_running:
            return
        pubsub = self.redis_client.pubsub()
        try:
            await pubsub.subscribe(self.channel)
            await asyncio.wait_for(self._confirmed(pubsub), _SUBSCRIBE_TIMEOUT)
        except BaseException:
            await _close(pubsub)
            raise
        self._pubsub = pubsub
        self._reader = asyncio.create_task(self._read())

    @staticmethod
    async def _confirmed(pubsub: Any) -> None:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=False, timeout=_READ_TIMEOUT)
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
                message = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=_READ_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("events: reading the events channel failed")
                await asyncio.sleep(_READ_TIMEOUT)
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
