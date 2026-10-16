"""`publish()` and the declared sources of custom topics."""

import logging

from typing import TYPE_CHECKING

from .event import Event
from .store import get_store
from .topics import SOURCE_NAME_PATTERN, validate_topic

if TYPE_CHECKING:
    from .authorizers import EventAuthorizer

logger = logging.getLogger(__name__)


async def publish(topic: str, event: Event) -> tuple[int, int]:
    """
    Publishes `event` to `topic` and returns the `(rev, prev)` the store assigned to it.

    Raises `ValueError` for a topic of neither form; the form is all that is checked. Records the
    event's `event_name`, payload and `event_version` with `store.append`, then carries them to
    every process that watches the topic with `store.publish`. An exception of `store.publish` is
    logged and not raised: the event is recorded, and a frontend learns of it on its next poll.
    """
    validate_topic(topic)
    if not isinstance(event, Event):
        raise TypeError(f"publish() takes an Event, not {type(event).__name__}")
    name = type(event).event_name
    version = type(event).event_version
    payload = event.model_dump(mode="json")
    store = get_store()
    rev, prev = await store.append(topic, name, payload, version)
    try:
        await store.publish(topic, rev, prev, name, payload, version)
    except Exception:
        logger.warning(
            "events: carrying %s rev %s of topic %s to the watching processes failed", name, rev, topic, exc_info=True
        )
    return rev, prev


_sources: dict[str, "Source"] = {}


class Source:
    """
    The declared owner of the custom topics `name` and `name:key`. Creating one registers `name` in
    this process; a name that does not match `[a-z0-9_.-]+`, or that is already declared, raises
    `ValueError`. `authorizer` is the `EventAuthorizer` `DeclaredSources()` consults for the
    source's topics.
    """

    def __init__(self, name: str, authorizer: "EventAuthorizer | None" = None):
        if not isinstance(name, str) or SOURCE_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(f"{name!r} is not a source name: it matches [a-z0-9_.-]+")
        if name in _sources:
            raise ValueError(f"source {name!r} is already declared")
        self.name = name
        self.authorizer = authorizer
        _sources[name] = self

    def topic(self, key: str | int | None = None) -> str:
        """`name:key`, or `name` without a key. Raises `ValueError` when the result is not a topic."""
        return validate_topic(self.name if key is None else f"{self.name}:{key}")

    async def publish(self, event: Event, key: str | int | None = None) -> tuple[int, int]:
        """`publish(self.topic(key), event)`."""
        return await publish(self.topic(key), event)

    async def drop(self, key: str | int) -> None:
        """Removes the rev and the log of the topic `name:key` from the store, for a topic that has no
        further events. Its next event, if any, carries `prev` 0."""
        await get_store().drop(self.topic(key))

    def __repr__(self) -> str:
        return f"Source({self.name!r})"


def source(name: str, authorizer: "EventAuthorizer | None" = None) -> Source:
    """Declares the source `name`; see `Source`."""
    return Source(name, authorizer)


def declared_source(name: str) -> Source | None:
    """The source declared as `name` in this process, or None."""
    return _sources.get(name)


def reset_sources() -> None:
    """Forgets every declared source."""
    _sources.clear()
