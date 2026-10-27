"""
`ApiErrorBody`, the body of every error response, and its construction from exceptions (`error_body()`).

Viewset routes answer with it without registration (`routing.TransportRoute`);
`install_error_handlers(app)` extends it to the application's other routes and unmatched requests.
"""

from http import HTTPStatus
from typing import Any, get_args

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.utils import is_body_allowed_for_status_code
from pydantic import BaseModel, Field
from pydantic_core.core_schema import ErrorType
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .error_namespace import _namespaced, ERROR_NAMESPACE_PYDANTIC
from .exceptions import DfViewSetError, UnhandledError


class ApiFieldError(BaseModel):
    """One failure of one value of a request that failed validation."""

    code: str = Field(
        description=(
            "pydantic's error type in the namespace `pydantic`, e.g. `pydantic.string_too_short`; "
            "`pydantic.custom_error` for a type pydantic does not declare (a `PydanticCustomError` of the "
            "application); `fastapi-viewsets.pk_assigned_by_server` for a primary key in a create's body."
        ),
        examples=["pydantic.string_too_short"],
    )
    params: dict[str, Any] = Field(
        description=(
            "The values pydantic built `detail` from (pydantic's `ctx`); `{}` without any. For "
            "`pydantic.custom_error`: `{type, ctx}`, the custom error's type and its `ctx`."
        ),
        examples=[{"min_length": 3}, {"type": "app.slug_taken", "ctx": {"slug": "blue"}}],
    )
    detail: str = Field(
        description="pydantic's English message (`msg`).",
        examples=["String should have at least 3 characters"],
    )


class ApiErrorBody(BaseModel):
    """The body of every error response."""

    detail: str = Field(
        description="English text of the error, interpolated from `detail_params`. Not translated.",
        examples=["Item with pk 42 not found"],
    )
    detail_code: str = Field(
        description=(
            "The error's full code, `<namespace>.<code>`: `fastapi-viewsets.<code>` for the errors the "
            "package raises, the application's own code as given for its own `DfViewSetError`."
        ),
        examples=["fastapi-viewsets.not_found"],
    )
    detail_params: dict[str, Any] = Field(
        description="The values `detail` was interpolated with, by name; `{}` without any.",
        examples=[{"pk": 42}],
    )
    field_errors: dict[str, list[ApiFieldError]] = Field(
        description=(
            "The failures of a request that failed validation (`fastapi-viewsets.validation_failed`), keyed "
            "by field name: pydantic's `loc` without its first element (`body`, `query`, `path`, `header`, "
            "`cookie`), joined with `.`; a failure of the body as a whole is keyed `''`. `{}` for every "
            "other error."
        ),
        examples=[{}],
    )


VALIDATION_FAILED_DETAIL = "The request failed validation"
"""The `detail` of `fastapi-viewsets.validation_failed`."""


def _jsonable_ctx(ctx: dict[str, Any] | None) -> dict[str, Any]:
    """pydantic's `ctx` as JSON values: an exception (the `error` of `value_error`) as its message."""
    return jsonable_encoder(
        {name: str(value) if isinstance(value, BaseException) else value for name, value in (ctx or {}).items()}
    )


PYDANTIC_ERROR_TYPES = frozenset(get_args(ErrorType))
"""The error types pydantic declares: the values of `pydantic_core.core_schema.ErrorType`."""

CUSTOM_ERROR_CODE = f"{ERROR_NAMESPACE_PYDANTIC}.custom_error"
"""The code of a field error whose type pydantic does not declare."""

_PACKAGE_FIELD_ERROR_CODES = {"pk_assigned_by_server": _namespaced("pk_assigned_by_server")}
"""The codes of the `PydanticCustomError` types this package raises, by type."""


def _field_error(error: Any) -> dict[str, Any]:
    error_type = error.get("type")
    ctx = _jsonable_ctx(error.get("ctx"))
    if error_type in PYDANTIC_ERROR_TYPES:
        code, params = f"{ERROR_NAMESPACE_PYDANTIC}.{error_type}", ctx
    elif error_type in _PACKAGE_FIELD_ERROR_CODES:
        code, params = _PACKAGE_FIELD_ERROR_CODES[error_type], ctx
    else:
        code, params = CUSTOM_ERROR_CODE, {"type": error_type, "ctx": ctx}
    return {"code": code, "params": params, "detail": str(error.get("msg", ""))}


def field_errors(errors: Any) -> dict[str, list[dict[str, Any]]]:
    """
    pydantic's errors as `ApiErrorBody.field_errors`, keyed by `loc` without its first element joined
    with `.` (`("body", "address", "city")` -> `address.city`). Declared pydantic types become
    `pydantic.<type>` with `ctx` as params, `pk_assigned_by_server` `fastapi-viewsets.pk_assigned_by_server`,
    unknown types `pydantic.custom_error` with `{type, ctx}`. `input` is left out.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for error in errors or ():
        loc = error.get("loc") or ()
        name = ".".join(str(part) for part in list(loc)[1:])
        grouped.setdefault(name, []).append(_field_error(error))
    return grouped


def _body(detail: str, code: str, params: dict[str, Any], fields: dict | None = None) -> dict[str, Any]:
    return {"detail": detail, "detail_code": code, "detail_params": params, "field_errors": fields or {}}


def error_status(error: BaseException) -> int:
    """An `HTTPException`'s own status, 422 for a `RequestValidationError`, otherwise 500."""
    if isinstance(error, HTTPException):
        return error.status_code
    if isinstance(error, RequestValidationError):
        return 422
    return 500


def error_body(error: BaseException) -> dict[str, Any]:
    """
    `error` as a JSON `ApiErrorBody`: `DfViewSetError` as is, `RequestValidationError` as
    `validation_failed`, other `HTTPException` as `http_error` (`{status}`, a non-string detail in
    `detail_params.detail`), anything else as `UnhandledError`.
    """
    if isinstance(error, RequestValidationError):
        return _body(VALIDATION_FAILED_DETAIL, _namespaced("validation_failed"), {}, field_errors(error.errors()))
    if not isinstance(error, HTTPException):
        error = UnhandledError()
    if isinstance(error, DfViewSetError):
        return _body(jsonable_encoder(error.detail), error.code, jsonable_encoder(error.params))
    params: dict[str, Any] = {"status": error.status_code}
    detail = error.detail
    if not isinstance(detail, str):
        params["detail"] = jsonable_encoder(detail)
        try:
            detail = HTTPStatus(error.status_code).phrase
        except ValueError:
            detail = f"HTTP {error.status_code}"
    return _body(detail, _namespaced("http_error"), params)


def error_response(error: BaseException) -> Response:
    """The JSON error response for `error`, with an `HTTPException`'s headers; no body for 204/304."""
    status = error_status(error)
    headers = getattr(error, "headers", None) if isinstance(error, HTTPException) else None
    if not is_body_allowed_for_status_code(status):
        return Response(status_code=status, headers=headers)
    return JSONResponse(error_body(error), status_code=status, headers=headers)


async def _handle_error(_request: Request, error: Exception) -> Response:
    """Handler used by `install_error_handlers` and viewset routes. Starlette re-raises an `Exception`
    after the response, so the server logs it."""
    return error_response(error)


def install_error_handlers(app: FastAPI) -> None:
    """
    Answers every error of `app`'s routes, and Starlette's 404/405, with an `ApiErrorBody`. Unhandled
    exceptions become 500 `unhandled_error` and are re-raised for logging. Handlers registered later,
    or for a more specific class or a status, take precedence.
    """
    app.add_exception_handler(DfViewSetError, _handle_error)
    app.add_exception_handler(HTTPException, _handle_error)
    app.add_exception_handler(RequestValidationError, _handle_error)
    app.add_exception_handler(Exception, _handle_error)


_FASTAPI_HANDLERS = (http_exception_handler, request_validation_exception_handler)
"""The handlers a FastAPI application registers by default."""


EXCEPTION_HANDLERS_SCOPE_KEY = "starlette.exception_handlers"


def uniform_exception_handlers(scope: dict[str, Any]) -> None:
    """
    For this request, replaces FastAPI's default `HTTPException` / `RequestValidationError` handlers
    with the `ApiErrorBody` one; the application's own handlers are kept.
    """
    exception_handlers, status_handlers = scope.get(EXCEPTION_HANDLERS_SCOPE_KEY) or ({}, {})
    handlers = {
        key: (_handle_error if handler in _FASTAPI_HANDLERS else handler) for key, handler in exception_handlers.items()
    }
    handlers.setdefault(HTTPException, _handle_error)
    handlers.setdefault(RequestValidationError, _handle_error)
    scope[EXCEPTION_HANDLERS_SCOPE_KEY] = (handlers, status_handlers)


def answers_unhandled_itself(app: Any) -> bool:
    """Whether `app` has a handler of its own, not `install_error_handlers`', for `Exception` or 500."""
    handlers = getattr(app, "exception_handlers", None) or {}
    return any(key in handlers and handlers[key] is not _handle_error for key in (Exception, 500))
