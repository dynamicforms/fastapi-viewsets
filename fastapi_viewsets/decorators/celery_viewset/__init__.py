import logging
import sys

from ..lifecycle_runner import LifecycleType
from .client import (
    celery_viewset_client,
    DEFAULT_RESULT_TIMEOUT,
    get_registered_queue_keys,
)
from .result_reader import (
    get_process_queue_key,
    get_result_queue_key,
    get_running_queue_keys,
    push_result,
    start_result_reader,
    stop_result_reader,
)
from .server import celery_viewset_server

logger = logging.getLogger(__name__)

# Allow explicit override via set_is_celery_worker()
_is_celery_worker: bool | None = None


def set_is_celery_worker(is_worker: bool) -> None:
    """Explicitly set whether we are running in a Celery worker context."""
    global _is_celery_worker
    _is_celery_worker = is_worker


def _detect_is_celery_worker() -> bool:
    """Auto-detect if we are running inside a Celery worker by inspecting sys.argv."""
    if _is_celery_worker is not None:
        return _is_celery_worker
    return len(sys.argv) > 0 and "celery" in sys.argv[0].lower()


def celery_viewset(
    celery_app,
    task_prefix: str,
    lifecycle: LifecycleType = "singleton",
    redis_client=None,
    result_timeout: float | None = DEFAULT_RESULT_TIMEOUT,
):
    """
    Convenience decorator that auto-detects whether to use client or server mode.

    - In a Celery worker (detected via sys.argv or set_is_celery_worker): uses celery_viewset_server
    - In a FastAPI app: uses celery_viewset_client

    For explicit control, use celery_viewset_client or celery_viewset_server directly. Both modes
    require redis_client.
    """
    if redis_client is None:
        raise ValueError("redis_client is required for celery_viewset")
    if _detect_is_celery_worker():
        return celery_viewset_server(celery_app, task_prefix, lifecycle=lifecycle, redis_client=redis_client)
    return celery_viewset_client(celery_app, task_prefix, redis_client=redis_client, result_timeout=result_timeout)


def check_result_readers() -> frozenset[str]:
    """
    Verify that every queue_key registered by a celery_viewset_client decorator (client/FastAPI
    mode only) has a running result reader task, logging a warning for each one that doesn't.

    celery_viewset_client decorators register their queue_key at import/decoration time, so by the
    time FastAPI startup code runs, every queue_key in use is already known - call this once, right
    after starting all result readers in the FastAPI lifespan. A queue_key with no running reader
    means requests dispatched to that celery_viewset wait for a result that never arrives until
    result_timeout passes (the Celery task itself runs fine - only the FastAPI-side pickup is missing).

    Returns the set of queue_keys that are missing a reader (empty if everything is running).
    Never raises - this is a diagnostic aid, not a correctness check enforced by the library.
    """
    missing = get_registered_queue_keys() - get_running_queue_keys()
    for queue_key in sorted(missing):
        logger.warning(
            "No result reader is running for queue_key=%s - requests dispatched to this "
            "celery_viewset wait for a result that never arrives until they time out. Call "
            "start_result_reader(redis_client, %r) in your FastAPI lifespan.",
            queue_key,
            queue_key,
        )
    return frozenset(missing)


__all__ = [
    "celery_viewset",
    "celery_viewset_client",
    "celery_viewset_server",
    "DEFAULT_RESULT_TIMEOUT",
    "set_is_celery_worker",
    "start_result_reader",
    "stop_result_reader",
    "check_result_readers",
    "push_result",
    "get_result_queue_key",
    "get_process_queue_key",
    "get_registered_queue_keys",
    "get_running_queue_keys",
]
