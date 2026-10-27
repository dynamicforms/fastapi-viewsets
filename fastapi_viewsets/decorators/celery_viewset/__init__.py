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

_is_celery_worker: bool | None = None


def set_is_celery_worker(is_worker: bool) -> None:
    """Overrides the worker detection of `celery_viewset`."""
    global _is_celery_worker
    _is_celery_worker = is_worker


def _detect_is_celery_worker() -> bool:
    """Whether this is a Celery worker: the `set_is_celery_worker` value, else "celery" in sys.argv[0]."""
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
    `celery_viewset_server` in a Celery worker, `celery_viewset_client` otherwise. Raises ValueError
    without `redis_client`.
    """
    if redis_client is None:
        raise ValueError("redis_client is required for celery_viewset")
    if _detect_is_celery_worker():
        return celery_viewset_server(celery_app, task_prefix, lifecycle=lifecycle, redis_client=redis_client)
    return celery_viewset_client(celery_app, task_prefix, redis_client=redis_client, result_timeout=result_timeout)


def check_result_readers() -> frozenset[str]:
    """
    Logs a warning for, and returns, every queue_key registered by `celery_viewset_client` without a
    running result reader; calls to it would wait until `result_timeout`. Call once in the FastAPI
    lifespan after starting the readers. Never raises.
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
