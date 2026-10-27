"""
The event store: it assigns each event of a topic its rev, keeps the topic's log for `poll`, and
carries a published event to `deliver()` in every process whose store is started.
"""

import time

from collections import deque
from dataclasses import dataclass, field
from typing import Any, NamedTuple, Protocol, runtime_checkable

from ..conf import settings

MAX_REV = 2**53
"""Exclusive bound of every rev, `Number.MAX_SAFE_INTEGER + 1`; `append` raises `OverflowError` at it."""


class LoggedEvent(NamedTuple):
    """One event of a topic's log."""

    rev: int
    prev: int
    """The topic's rev before this event, 0 for a topic without one."""
    name: str
    payload: Any
    version: int
    """The event's `event_version`."""


@runtime_checkable
class EventStore(Protocol):
    async def start(self) -> None:
        """Starts what the store needs to carry events to this process."""

    async def stop(self) -> None:
        """Stops what `start` started."""

    async def append(self, topic: str, name: str, payload: Any, version: int) -> tuple[int, int]:
        """Sets the topic's rev to `max(rev + 1, now in milliseconds)`, keeps the event's name,
        payload and version in the topic's log and returns `(rev, prev)`. Atomic; raises
        `OverflowError` when the rev would reach `MAX_REV`."""

    async def revs(self, topics: list[str]) -> dict[str, int]:
        """The current rev of each topic, 0 for a topic without one."""

    async def since(self, topic: str, after: int) -> list[LoggedEvent] | None:
        """`[]` when the topic's rev is `after`; otherwise the kept event whose `prev` is `after` and
        every newer one, oldest first; None when no kept event has that `prev`."""

    async def publish(self, topic: str, rev: int, prev: int, name: str, payload: Any, version: int) -> None:
        """Carries the event to `deliver(topic, rev, prev, name, payload, version)` in every process
        whose store is started; `deliver()` pushes it to the subscriptions of the topic there."""

    async def drop(self, topic: str) -> None:
        """Removes the topic's rev and log."""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def next_rev(prev: int, now_ms: int) -> int:
    """`max(prev + 1, now_ms)`; raises `OverflowError` when that reaches `MAX_REV`."""
    rev = max(prev + 1, now_ms)
    if rev >= MAX_REV:
        raise OverflowError(f"rev {rev} reaches 2**53, which a JSON number on the frontend cannot hold")
    return rev


@dataclass
class _Topic:
    rev: int = 0
    log: deque[LoggedEvent] = field(default_factory=deque)
    log_expires_ms: int = 0


class MemoryEventStore:
    """
    The event store of one process: an event published in another process (a Celery worker) never reaches it.

    A topic's log keeps its last `settings.viewsets_event_log_size` events and empties once
    `settings.viewsets_event_log_ttl` seconds pass after the last event; the rev is kept until `drop`.
    `append` has no `await` between reading and writing the rev, so concurrent appends get distinct revs.
    """

    def __init__(self) -> None:
        self._topics: dict[str, _Topic] = {}

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def _log(self, state: _Topic, now_ms: int) -> deque[LoggedEvent]:
        if state.log and now_ms >= state.log_expires_ms:
            state.log.clear()
        return state.log

    async def append(self, topic: str, name: str, payload: Any, version: int) -> tuple[int, int]:
        now_ms = _now_ms()
        state = self._topics.get(topic)
        prev = state.rev if state is not None else 0
        rev = next_rev(prev, now_ms)
        if state is None:
            state = self._topics[topic] = _Topic()
        state.rev = rev
        log = self._log(state, now_ms)
        log.append(LoggedEvent(rev, prev, name, payload, version))
        while len(log) > max(settings.viewsets_event_log_size, 0):
            log.popleft()
        state.log_expires_ms = now_ms + int(settings.viewsets_event_log_ttl * 1000)
        return rev, prev

    async def revs(self, topics: list[str]) -> dict[str, int]:
        return {topic: self._topics[topic].rev if topic in self._topics else 0 for topic in topics}

    async def since(self, topic: str, after: int) -> list[LoggedEvent] | None:
        state = self._topics.get(topic)
        rev = state.rev if state is not None else 0
        if after == rev:
            return []
        if state is None:
            return None
        chain: list[LoggedEvent] = []
        for entry in reversed(self._log(state, _now_ms())):
            chain.append(entry)
            if entry.prev == after:
                chain.reverse()
                return chain
        return None

    async def publish(self, topic: str, rev: int, prev: int, name: str, payload: Any, version: int) -> None:
        from .delivery import deliver

        await deliver(topic, rev, prev, name, payload, version)

    async def drop(self, topic: str) -> None:
        self._topics.pop(topic, None)


_default_store: MemoryEventStore | None = None


def get_store() -> EventStore:
    """`settings.viewsets_event_store`, or this process's one `MemoryEventStore` while that is None."""
    global _default_store
    store = settings.viewsets_event_store
    if store is not None:
        return store
    if _default_store is None:
        _default_store = MemoryEventStore()
    return _default_store
