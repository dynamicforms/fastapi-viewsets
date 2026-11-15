"""
`ApiErrorBody`, the body of every error response, and its construction from exceptions (`error_body()`).

Viewset routes answer with it without registration (`routing.TransportRoute`);
`install_error_handlers(app)` extends it to the application's other routes and unmatched requests.
"""

import datetime

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from http import HTTPStatus
from types import MappingProxyType
from typing import Any, get_args
from uuid import UUID

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

from .error_namespace import _namespaced, ERROR_NAMESPACE_DJANGO, ERROR_NAMESPACE_PYDANTIC
from .exceptions import DfViewSetError, UnhandledError

_FIELD_CODE_DESCRIPTION = (
    "The failure's full code. This package's own codes, with their params: "
    "`fastapi-viewsets.required` {} (no value, or null where null is not allowed); "
    "`fastapi-viewsets.min_length` {minLength} and `fastapi-viewsets.max_length` {maxLength} (characters, "
    "items or bytes); `fastapi-viewsets.min_value` {minValue} and `fastapi-viewsets.max_value` {maxValue} "
    "(inclusive bounds); `fastapi-viewsets.min_value_exclusive` {minValue} and "
    "`fastapi-viewsets.max_value_exclusive` {maxValue} (exclusive bounds; a bound is a number, or an ISO "
    "string for a date or time); `fastapi-viewsets.multiple_of` {multipleOf}; `fastapi-viewsets.max_digits` "
    "{maxDigits}; `fastapi-viewsets.max_decimal_places` {maxDecimalPlaces}; "
    "`fastapi-viewsets.max_whole_digits` {maxWholeDigits}; `fastapi-viewsets.pattern` {pattern} (the "
    "regular expression's source); `fastapi-viewsets.in_allowed_values` {allowedAsText} (pydantic's text "
    "of the allowed values, e.g. `'a', 'b' or 'c'`); `fastapi-viewsets.invalid_choice` {} (not an allowed "
    "value; the values are not in params); `fastapi-viewsets.invalid_integer`, `invalid_number`, `invalid_boolean`, "
    "`invalid_date`, `invalid_time`, `invalid_datetime`, `invalid_duration`, `invalid_url`, `invalid_uuid` "
    "{} (not a value of that kind); `fastapi-viewsets.past` {} and `fastapi-viewsets.future` {}; "
    "`fastapi-viewsets.invalid_type` {} (wrong JSON or structural type); `fastapi-viewsets.unique` {fields} "
    "(a row with the same values in these Django model fields exists); `fastapi-viewsets.invalid` {} (the reason is "
    "in `detail`); `fastapi-viewsets.pk_assigned_by_server` {} (a primary key in a create's body). Any other "
    "pydantic type is `pydantic.<type>` with pydantic's `ctx` as params; any other Django code is "
    "`django.<code>` with Django's params; a custom type or Django code with a namespace (`app.slug_taken`) "
    "is the code unchanged, with its params; a custom type without a namespace is `pydantic.custom_error` "
    "with params `{type, ctx}`."
)


class ApiFieldError(BaseModel):
    """One failure of one value of a request that failed validation."""

    code: str = Field(description=_FIELD_CODE_DESCRIPTION, examples=["fastapi-viewsets.min_length"])
    params: dict[str, Any] = Field(
        description=(
            "The values of the failure, by name: the params `code` declares for this package's codes; "
            "pydantic's `ctx` or Django's params for the others (`{type, ctx}` for `pydantic.custom_error`); "
            "`{}` without any."
        ),
        examples=[{"minLength": 3}, {"slug": "blue"}],
    )
    detail: str = Field(
        description=(
            "The failure's message: pydantic's `msg` (English, except the application's text in a custom error, "
            "`value_error` or `assertion_error`), or Django's message in Django's active language."
        ),
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
            "`cookie`), joined with `.`; a failure of the body as a whole is keyed `''`. A Django "
            "`ValidationError` is keyed by Django field name (`''` for non-field errors), prefixed `<pk>.` in "
            "a bulk update. On a viewset's endpoint, the viewset can rename each key. `{}` for every other error."
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
"""The code of a field error from a custom error type that pydantic does not declare and that has no namespace."""

FIELD_ERROR_PARAMS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        _namespaced("required"): (),
        _namespaced("min_length"): ("minLength",),
        _namespaced("max_length"): ("maxLength",),
        _namespaced("min_value"): ("minValue",),
        _namespaced("min_value_exclusive"): ("minValue",),
        _namespaced("max_value"): ("maxValue",),
        _namespaced("max_value_exclusive"): ("maxValue",),
        _namespaced("multiple_of"): ("multipleOf",),
        _namespaced("max_digits"): ("maxDigits",),
        _namespaced("max_decimal_places"): ("maxDecimalPlaces",),
        _namespaced("max_whole_digits"): ("maxWholeDigits",),
        _namespaced("pattern"): ("pattern",),
        _namespaced("in_allowed_values"): ("allowedAsText",),
        _namespaced("invalid_choice"): (),
        _namespaced("invalid_integer"): (),
        _namespaced("invalid_number"): (),
        _namespaced("invalid_boolean"): (),
        _namespaced("invalid_date"): (),
        _namespaced("invalid_time"): (),
        _namespaced("invalid_datetime"): (),
        _namespaced("invalid_duration"): (),
        _namespaced("invalid_url"): (),
        _namespaced("invalid_uuid"): (),
        _namespaced("past"): (),
        _namespaced("future"): (),
        _namespaced("invalid_type"): (),
        _namespaced("unique"): ("fields",),
        _namespaced("invalid"): (),
        _namespaced("pk_assigned_by_server"): (),
    }
)
"""The field error codes this package declares, each with the names of its params, in order."""

_ParamSources = dict[str, tuple[str, ...]]
"""Param name -> candidate keys in the source (pydantic's `ctx`, Django's `params`); the first key present is read."""


def _codes(code: str, sources: _ParamSources, *names: str) -> dict[str, tuple[str, _ParamSources]]:
    return dict.fromkeys(names, (code, sources))


_PYDANTIC_FIELD_ERRORS: dict[str, tuple[str, _ParamSources]] = {
    **_codes(
        _namespaced("required"),
        {},
        "missing",
        "missing_argument",
        "missing_keyword_only_argument",
        "missing_positional_only_argument",
    ),
    **_codes(
        _namespaced("min_length"), {"minLength": ("min_length",)}, "string_too_short", "too_short", "bytes_too_short"
    ),
    **_codes(
        _namespaced("max_length"),
        {"maxLength": ("max_length",)},
        "string_too_long",
        "too_long",
        "bytes_too_long",
        "url_too_long",
    ),
    **_codes(_namespaced("min_value"), {"minValue": ("ge",)}, "greater_than_equal"),
    **_codes(_namespaced("min_value_exclusive"), {"minValue": ("gt",)}, "greater_than"),
    **_codes(_namespaced("max_value"), {"maxValue": ("le",)}, "less_than_equal"),
    **_codes(_namespaced("max_value_exclusive"), {"maxValue": ("lt",)}, "less_than"),
    **_codes(_namespaced("multiple_of"), {"multipleOf": ("multiple_of",)}, "multiple_of"),
    **_codes(_namespaced("max_digits"), {"maxDigits": ("max_digits",)}, "decimal_max_digits"),
    **_codes(_namespaced("max_decimal_places"), {"maxDecimalPlaces": ("decimal_places",)}, "decimal_max_places"),
    **_codes(_namespaced("max_whole_digits"), {"maxWholeDigits": ("whole_digits",)}, "decimal_whole_digits"),
    **_codes(_namespaced("pattern"), {"pattern": ("pattern",)}, "string_pattern_mismatch"),
    **_codes(_namespaced("in_allowed_values"), {"allowedAsText": ("expected",)}, "enum", "literal_error"),
    **_codes(_namespaced("invalid_integer"), {}, "int_type", "int_parsing", "int_parsing_size", "int_from_float"),
    **_codes(
        _namespaced("invalid_number"),
        {},
        "float_type",
        "float_parsing",
        "finite_number",
        "decimal_type",
        "decimal_parsing",
        "fraction_type",
        "fraction_parsing",
    ),
    **_codes(_namespaced("invalid_boolean"), {}, "bool_type", "bool_parsing"),
    **_codes(
        _namespaced("invalid_date"),
        {},
        "date_type",
        "date_parsing",
        "date_from_datetime_parsing",
        "date_from_datetime_inexact",
    ),
    **_codes(_namespaced("invalid_time"), {}, "time_type", "time_parsing"),
    **_codes(
        _namespaced("invalid_datetime"),
        {},
        "datetime_type",
        "datetime_parsing",
        "datetime_object_invalid",
        "datetime_from_date_parsing",
    ),
    **_codes(_namespaced("invalid_duration"), {}, "time_delta_type", "time_delta_parsing"),
    **_codes(_namespaced("invalid_url"), {}, "url_type", "url_parsing", "url_syntax_violation"),
    **_codes(_namespaced("invalid_uuid"), {}, "uuid_type", "uuid_parsing"),
    **_codes(_namespaced("past"), {}, "date_past", "datetime_past"),
    **_codes(_namespaced("future"), {}, "date_future", "datetime_future"),
    **_codes(
        _namespaced("invalid_type"),
        {},
        "string_type",
        "string_unicode",
        "bytes_type",
        "dict_type",
        "frozen_dict_type",
        "ordered_dict_type",
        "counter_type",
        "list_type",
        "tuple_type",
        "set_type",
        "frozen_set_type",
        "deque_type",
        "mapping_type",
        "iterable_type",
        "model_type",
        "model_attributes_type",
        "dataclass_type",
        "dataclass_exact_type",
        "named_tuple_type",
        "none_required",
        "arguments_type",
        "callable_type",
        "is_instance_of",
        "is_subclass_of",
    ),
    **_codes(_namespaced("invalid"), {}, "value_error", "assertion_error"),
}
"""pydantic's error types this package's field error codes cover: type -> (code, param sources in `ctx`)."""

_NONE_IS_REQUIRED = frozenset(
    error_type
    for error_type, (code, _sources) in _PYDANTIC_FIELD_ERRORS.items()
    if code.rpartition(".")[2].startswith("invalid_")
    or code in {_namespaced("invalid_type"), _namespaced("in_allowed_values")}
)
"""The pydantic types that become `required` when their input is null."""

_DJANGO_FIELD_ERRORS: dict[str, tuple[str, _ParamSources]] = {
    **_codes(_namespaced("required"), {}, "required", "blank", "null"),
    **_codes(_namespaced("max_length"), {"maxLength": ("limit_value", "max")}, "max_length"),
    **_codes(_namespaced("min_length"), {"minLength": ("limit_value",)}, "min_length"),
    **_codes(_namespaced("max_value"), {"maxValue": ("limit_value",)}, "max_value"),
    **_codes(_namespaced("min_value"), {"minValue": ("limit_value",)}, "min_value"),
    **_codes(_namespaced("multiple_of"), {"multipleOf": ("limit_value",)}, "step_size"),
    **_codes(_namespaced("max_digits"), {"maxDigits": ("max",)}, "max_digits"),
    **_codes(_namespaced("max_decimal_places"), {"maxDecimalPlaces": ("max",)}, "max_decimal_places"),
    **_codes(_namespaced("max_whole_digits"), {"maxWholeDigits": ("max",)}, "max_whole_digits"),
    **_codes(_namespaced("invalid_choice"), {}, "invalid_choice"),
    **_codes(_namespaced("invalid_date"), {}, "invalid_date"),
    **_codes(_namespaced("invalid_time"), {}, "invalid_time"),
    **_codes(_namespaced("invalid_datetime"), {}, "invalid_datetime"),
    **_codes(_namespaced("unique"), {"fields": ("unique_check",)}, "unique", "unique_together"),
    **_codes(_namespaced("invalid"), {}, "invalid"),
}
"""Django's validation codes this package's field error codes cover: code -> (code, param sources in `params`)."""

_MISSING = object()


def _read_params(source: Mapping[str, Any], sources: _ParamSources, convert: Any) -> dict[str, Any] | None:
    """The params `sources` names, read from `source` and converted; None when one is missing."""
    params = {}
    for name, keys in sources.items():
        key = next((key for key in keys if key in source), None)
        value = _MISSING if key is None else convert(source[key])
        if value is _MISSING:
            return None
        params[name] = value
    return params


def _jsonable_value(value: Any) -> Any:
    return jsonable_encoder(str(value) if isinstance(value, BaseException) else value)


def _source_code(error: Any) -> str:
    """
    The code of one pydantic-style error before this package maps it: `pydantic.<type>` for a type pydantic
    declares, a namespaced type (`django.unique`, `app.slug_taken`) as is, `pydantic.custom_error` for any other.
    """
    error_type = error.get("type")
    if error_type in PYDANTIC_ERROR_TYPES:
        return f"{ERROR_NAMESPACE_PYDANTIC}.{error_type}"
    if isinstance(error_type, str) and "." in error_type:
        return error_type
    return CUSTOM_ERROR_CODE


def _field_error(error: Any) -> dict[str, Any]:
    error_type = error.get("type")
    detail = str(error.get("msg", ""))
    django_code = _django_code(error_type)
    if django_code is not None:
        ctx = _jsonable_ctx(error.get("ctx"))
        if django_code in _DJANGO_FIELD_ERRORS and not (django_code == "step_size" and "offset" in ctx):
            code, sources = _DJANGO_FIELD_ERRORS[django_code]
            params = _read_params(ctx, sources, lambda value: value)
            if params is not None:
                return {"code": code, "params": params, "detail": detail}
        return {"code": error_type, "params": ctx, "detail": detail}
    if error_type in _PYDANTIC_FIELD_ERRORS:
        code, sources = _PYDANTIC_FIELD_ERRORS[error_type]
        if error_type in _NONE_IS_REQUIRED and "input" in error and error["input"] is None:
            return {"code": _namespaced("required"), "params": {}, "detail": detail}
        params = _read_params(error.get("ctx") or {}, sources, _jsonable_value)
        if params is not None:
            return {"code": code, "params": params, "detail": detail}
    ctx = _jsonable_ctx(error.get("ctx"))
    if error_type in PYDANTIC_ERROR_TYPES:
        code, params = f"{ERROR_NAMESPACE_PYDANTIC}.{error_type}", ctx
    elif isinstance(error_type, str) and "." in error_type:
        code, params = error_type, ctx
    else:
        code, params = CUSTOM_ERROR_CODE, {"type": error_type, "ctx": ctx}
    return {"code": code, "params": params, "detail": detail}


@dataclass(frozen=True)
class FieldNames:
    """How `field_errors()` names the fields of a viewset's errors."""

    rename: Callable[[str, str], str]
    """(field, source code) -> the field name to use."""
    row_keyed: bool = False
    """Whether the first element after `body` in a `body` loc is a row's index or primary key. It is kept in
    front of the renamed field and not passed to `rename`. A loc that does not start with `body` has no
    row's key."""


def field_errors(errors: Any, names: FieldNames | None = None) -> dict[str, list[dict[str, Any]]]:
    """
    pydantic's errors as `ApiErrorBody.field_errors`, keyed by `loc` without its first element joined
    with `.` (`("body", "address", "city")` -> `address.city`). A pydantic type that has a field error
    code becomes that code, with params read from `ctx`; a type or choice error with a null input becomes
    `required`. Any other pydantic type, or a covered type whose `ctx` lacks a param key, becomes
    `pydantic.<type>` with `ctx` as params. A `django.<code>` type from `_django_errors()` becomes its
    field error code under the same rules. A namespaced type (`app.slug_taken`) is the code, with `ctx`
    as params. Any other type becomes `pydantic.custom_error` with `{type, ctx}`. `input` is left out.

    With `names`, each key is `names.rename(field, source code)`, the row's key kept in front of it when
    `names.row_keyed`.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for error in errors or ():
        loc = list(error.get("loc") or ())
        parts = [str(part) for part in loc[1:]]
        row = None
        if names is not None and names.row_keyed and loc[:1] == ["body"] and parts:
            row, parts = parts[0], parts[1:]
        name = ".".join(parts)
        if names is not None:
            name = names.rename(name, _source_code(error))
        if row is not None:
            name = f"{row}.{name}" if name else row
        grouped.setdefault(name, []).append(_field_error(error))
    return grouped


@cache
def _django_validation_error() -> type[Exception] | None:
    """`django.core.exceptions.ValidationError`; None when Django is not installed."""
    try:
        from django.core.exceptions import ValidationError
    except ImportError:  # pragma: no cover - the django extra is not installed
        return None
    return ValidationError


def _is_django_validation_error(error: BaseException) -> bool:
    validation_error = _django_validation_error()
    return validation_error is not None and isinstance(error, validation_error)


def _json_safe(value: Any) -> Any:
    """
    `value` as JSON: lazy text as a string; dates, times, durations, decimals and UUIDs through
    `jsonable_encoder`; containers element by element. Any other object is `_MISSING`; containers drop it.
    """
    from django.utils.functional import Promise

    if isinstance(value, Promise):
        return str(value)
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, (datetime.date, datetime.time, datetime.timedelta, Decimal, UUID)):
        return jsonable_encoder(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [item for item in map(_json_safe, value) if item is not _MISSING]
    if isinstance(value, dict):
        return _json_safe_dict(value)
    return _MISSING


def _json_safe_dict(values: Mapping[Any, Any]) -> dict[str, Any]:
    safe = {str(key): _json_safe(value) for key, value in values.items()}
    return {key: value for key, value in safe.items() if value is not _MISSING}


def _django_code(error_type: Any) -> str | None:
    """The Django code of a type in the `django` namespace; None for any other type."""
    prefix = f"{ERROR_NAMESPACE_DJANGO}."
    if isinstance(error_type, str) and error_type.startswith(prefix):
        return error_type[len(prefix) :]
    return None


def _django_message(message: Any, params: Mapping[str, Any]) -> str:
    """`message` with `params` substituted; `message` as is when it does not format with them."""
    if not params:
        return str(message)
    try:
        return str(message % params)
    except (KeyError, ValueError, TypeError):
        return str(message)


def _django_errors(error: Exception, loc: tuple[Any, ...] = (ERROR_NAMESPACE_DJANGO,)) -> list[dict[str, Any]]:
    """
    A Django `ValidationError` as pydantic-style errors for `field_errors()`: `loc` plus the field name
    (none for `NON_FIELD_ERRORS` or an error without fields), `type` the Django code as `django.<code>`
    (`django.invalid` without a code; a namespaced code as is), `ctx` its params as JSON, `msg` the message
    with its params substituted. The default `loc` `("django",)` holds no row's key, so `field_errors()`
    does not split one off in a bulk action.
    """
    from django.core.exceptions import NON_FIELD_ERRORS

    groups = error.error_dict.items() if hasattr(error, "error_dict") else [(NON_FIELD_ERRORS, error.error_list)]
    entries = []
    for field, grouped in groups:
        field_loc = loc if field in (NON_FIELD_ERRORS, None) else (*loc, str(field))
        for one in (single for errors in grouped for single in errors.error_list):
            params = one.params or {}
            code = one.code or "invalid"
            error_type = code if "." in code else f"{ERROR_NAMESPACE_DJANGO}.{code}"
            message = _django_message(one.message, params)
            entries.append({"type": error_type, "loc": field_loc, "msg": message, "ctx": _json_safe_dict(params)})
    return entries


def _body(detail: str, code: str, params: dict[str, Any], fields: dict | None = None) -> dict[str, Any]:
    return {"detail": detail, "detail_code": code, "detail_params": params, "field_errors": fields or {}}


def error_status(error: BaseException) -> int:
    """
    An `HTTPException`'s own status, 422 for a `RequestValidationError` or Django's `ValidationError`,
    otherwise 500.
    """
    if isinstance(error, HTTPException):
        return error.status_code
    if isinstance(error, RequestValidationError) or _is_django_validation_error(error):
        return 422
    return 500


def error_body(error: BaseException, names: FieldNames | None = None) -> dict[str, Any]:
    """
    `error` as a JSON `ApiErrorBody`: `DfViewSetError` as is, `RequestValidationError` and Django's
    `ValidationError` as `validation_failed` (field names through `names`), other `HTTPException` as
    `http_error` (`{status}`, a non-string detail in `detail_params.detail`), anything else as `UnhandledError`.
    """
    if isinstance(error, RequestValidationError) or _is_django_validation_error(error):
        errors = error.errors() if isinstance(error, RequestValidationError) else _django_errors(error)
        return _body(VALIDATION_FAILED_DETAIL, _namespaced("validation_failed"), {}, field_errors(errors, names))
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


def error_response(error: BaseException, names: FieldNames | None = None) -> Response:
    """The JSON error response for `error`, with an `HTTPException`'s headers; no body for 204/304."""
    status = error_status(error)
    headers = getattr(error, "headers", None) if isinstance(error, HTTPException) else None
    if not is_body_allowed_for_status_code(status):
        return Response(status_code=status, headers=headers)
    return JSONResponse(error_body(error, names), status_code=status, headers=headers)


FIELD_NAMES_ATTRIBUTE = "__viewset_field_names__"
"""Set by `route_viewset` on a viewset endpoint: an async function of the request returning its `FieldNames`."""


async def _handle_error(request: Request, error: Exception) -> Response:
    """Handler used by `install_error_handlers` and viewset routes. The field errors of a viewset route are
    named by its viewset. Starlette re-raises an `Exception` after the response, so the server logs it."""
    names = None
    if isinstance(error, RequestValidationError) or _is_django_validation_error(error):
        resolve = getattr(request.scope.get("endpoint"), FIELD_NAMES_ATTRIBUTE, None)
        if resolve is not None:
            names = await resolve(request)
    return error_response(error, names)


def install_error_handlers(app: FastAPI) -> None:
    """
    Answers every error of `app`'s routes, and Starlette's 404/405, with an `ApiErrorBody`; Django's
    `ValidationError` (when Django is installed) becomes 422 `validation_failed`. Unhandled
    exceptions become 500 `unhandled_error` and are re-raised for logging. Handlers registered later,
    or for a more specific class or a status, take precedence.
    """
    app.add_exception_handler(DfViewSetError, _handle_error)
    app.add_exception_handler(HTTPException, _handle_error)
    app.add_exception_handler(RequestValidationError, _handle_error)
    if (django_validation_error := _django_validation_error()) is not None:
        app.add_exception_handler(django_validation_error, _handle_error)
    app.add_exception_handler(Exception, _handle_error)


_FASTAPI_HANDLERS = (http_exception_handler, request_validation_exception_handler)
"""The handlers a FastAPI application registers by default."""


EXCEPTION_HANDLERS_SCOPE_KEY = "starlette.exception_handlers"


def uniform_exception_handlers(scope: dict[str, Any]) -> None:
    """
    For this request, replaces FastAPI's default `HTTPException` / `RequestValidationError` handlers
    with the `ApiErrorBody` one and adds it for Django's `ValidationError`; the application's own
    handlers are kept.
    """
    exception_handlers, status_handlers = scope.get(EXCEPTION_HANDLERS_SCOPE_KEY) or ({}, {})
    handlers = {
        key: (_handle_error if handler in _FASTAPI_HANDLERS else handler) for key, handler in exception_handlers.items()
    }
    handlers.setdefault(HTTPException, _handle_error)
    handlers.setdefault(RequestValidationError, _handle_error)
    if (django_validation_error := _django_validation_error()) is not None:
        handlers.setdefault(django_validation_error, _handle_error)
    scope[EXCEPTION_HANDLERS_SCOPE_KEY] = (handlers, status_handlers)


def answers_unhandled_itself(app: Any) -> bool:
    """Whether `app` has a handler of its own, not `install_error_handlers`', for `Exception` or 500."""
    handlers = getattr(app, "exception_handlers", None) or {}
    return any(key in handlers and handlers[key] is not _handle_error for key in (Exception, 500))
