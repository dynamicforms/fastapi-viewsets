"""
The HTTP exceptions this package raises on its own behalf.

`detail` is the English default, fully interpolated - a plain string, so an application that does
nothing further receives FastAPI's usual `{"detail": "..."}`. `code` names what went
wrong independently of `detail`'s wording, and `params` are the raw values `detail` was
interpolated with; both reach the response body only once the application registers
`df_viewset_exception_handler` for `DfViewSetError` (see that function). A view's own
`raise HTTPException(status_code, detail="...")` is unaffected either way.
"""

from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .cursor import CursorError


class DfViewSetError(HTTPException):
    """Base for every `HTTPException` this package raises on its own behalf."""

    def __init__(self, status_code: int, message: str, code: str, params: dict[str, Any] | None = None):
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.params = params or {}


class NotFoundError(DfViewSetError):
    def __init__(self, pk: Any):
        super().__init__(404, f"Item with pk {pk} not found", "not_found", {"pk": pk})


class ReadOnlyCollectionError(DfViewSetError):
    """A write to a `CollectionViewSet` whose container is immutable (a tuple, a frozenset, a
    read-only mapping)."""

    def __init__(self):
        super().__init__(405, "This collection is read-only", "read_only")


class SessionExpiredError(DfViewSetError):
    def __init__(self):
        super().__init__(401, "Session expired or invalid", "session_expired")


class NotAuthorizedError(DfViewSetError):
    def __init__(self):
        super().__init__(403, "Not authorized to perform this action", "not_authorized")


class RateLimitedError(DfViewSetError):
    def __init__(self):
        super().__init__(429, "Rate limit exceeded", "rate_limited")


class ResultTimeoutError(DfViewSetError):
    """A `celery_viewset` call whose result did not arrive from the worker within `timeout` seconds."""

    def __init__(self, task: str, timeout: float):
        message = f"No result for {task} within {timeout} s"
        super().__init__(504, message, "result_timeout", {"task": task, "timeout": timeout})


class ResultReaderStoppedError(DfViewSetError):
    """A `celery_viewset` call that was pending when the result reader of its `queue_key` stopped."""

    def __init__(self, queue_key: str):
        message = f"The result reader for {queue_key} stopped before the result arrived"
        super().__init__(503, message, "result_reader_stopped", {"queue_key": queue_key})


class IdempotencyKeyInvalidError(DfViewSetError):
    """An `Idempotency-Key` header that is not a single, non-empty Structured Field string."""

    def __init__(self):
        super().__init__(400, "Idempotency-Key must be a non-empty Structured Field string", "idempotency_key_invalid")


class IdempotencyKeyReusedError(DfViewSetError):
    """An `Idempotency-Key` already recorded for a call with a different method, path, query or body."""

    def __init__(self, key: str):
        message = f"Idempotency key {key} was used for a different request"
        super().__init__(422, message, "idempotency_key_reused", {"key": key})


class UnsupportedListShapeError(DfViewSetError):
    def __init__(self, shape: str, allowed: list[str]):
        message = f"unsupported list shape {shape!r}; this endpoint offers {', '.join(allowed)}"
        super().__init__(422, message, "unsupported_list_shape", {"shape": shape, "allowed": allowed})


class CursorRequestError(DfViewSetError):
    """Raised in place of a caught `CursorError`, carrying the same `code`/`params` as a 400 response."""

    def __init__(self, error: CursorError):
        super().__init__(400, str(error), error.code, error.params)


async def df_viewset_exception_handler(_request: Request, exc: DfViewSetError) -> JSONResponse:
    """
    Adds `detail_code` and `detail_params` alongside `detail` in the response body.

    Opt in once, for `DfViewSetError` - every exception this package raises on its own descends
    from it:

        from fastapi_viewsets.exceptions import DfViewSetError, df_viewset_exception_handler

        app.add_exception_handler(DfViewSetError, df_viewset_exception_handler)

    An application that never registers this receives `{"detail": "..."}`, from FastAPI's own
    default `HTTPException` handling; `code` and `params` reach nothing without it.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "detail_code": exc.code, "detail_params": exc.params},
        headers=exc.headers,
    )
