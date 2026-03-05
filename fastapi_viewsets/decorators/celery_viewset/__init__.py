import sys

from ..lifecycle_runner import LifecycleType
from .client import celery_viewset_client
from .result_reader import get_result_queue_key, push_result, start_result_reader, stop_result_reader
from .server import celery_viewset_server

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


def celery_viewset(celery_app, task_prefix: str, lifecycle: LifecycleType = "singleton", redis_client=None):
    """
    Convenience decorator that auto-detects whether to use client or server mode.

    - In a Celery worker (detected via sys.argv or set_is_celery_worker): uses celery_viewset_server
    - In a FastAPI app: uses celery_viewset_client

    For explicit control, use celery_viewset_client or celery_viewset_server directly.
    """
    if _detect_is_celery_worker():
        return celery_viewset_server(celery_app, task_prefix, lifecycle=lifecycle, redis_client=redis_client)
    else:
        if redis_client is None:
            raise ValueError("redis_client is required for celery_viewset in client (FastAPI) mode")
        return celery_viewset_client(celery_app, task_prefix, redis_client=redis_client)


__all__ = [
    "celery_viewset",
    "celery_viewset_client",
    "celery_viewset_server",
    "set_is_celery_worker",
    "start_result_reader",
    "stop_result_reader",
    "push_result",
    "get_result_queue_key",
]
