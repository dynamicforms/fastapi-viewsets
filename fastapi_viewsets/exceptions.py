"""
The HTTP exceptions this package raises on its own behalf.

`detail` is the English default, fully interpolated - a plain string, exactly as it always was, so
an application that does nothing further sees exactly what it always has. `code` names what went
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


class SessionExpiredError(DfViewSetError):
    def __init__(self):
        super().__init__(401, "Session expired or invalid", "session_expired")


class NotAuthorizedError(DfViewSetError):
    def __init__(self):
        super().__init__(403, "Not authorized to perform this action", "not_authorized")


class RateLimitedError(DfViewSetError):
    def __init__(self):
        super().__init__(429, "Rate limit exceeded", "rate_limited")


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

    An application that never registers this sees exactly what it always has -
    `{"detail": "..."}`, from FastAPI's own default `HTTPException` handling; `code` and `params`
    reach nothing without it.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "detail_code": exc.code, "detail_params": exc.params},
        headers=exc.headers,
    )
