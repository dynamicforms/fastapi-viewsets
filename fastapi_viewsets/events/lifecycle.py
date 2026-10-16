"""Starting and stopping the event store from the application's lifespan."""

import logging

from .store import get_store

logger = logging.getLogger(__name__)


async def start_events() -> None:
    """Starts the configured store: a `RedisEventStore` starts its reader, which carries every event
    to `deliver()`. Called from the application's lifespan."""
    await get_store().start()


async def stop_events() -> None:
    """Stops what `start_events()` started. Called from the application's lifespan."""
    await get_store().stop()


def check_events() -> bool:
    """Logs a warning and returns False when the configured store needs a reader and none runs, a
    `RedisEventStore` before `start_events()`; returns True otherwise."""
    store = get_store()
    if getattr(store, "reader_running", True) is False:
        logger.warning(
            "events: %s has no running reader, so no push reaches this process's sockets; "
            "call `await start_events()` in the application's lifespan",
            type(store).__name__,
        )
        return False
    return True
