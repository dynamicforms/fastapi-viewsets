"""
The OpenAPI `responses` entries of the error responses of a viewset's endpoints. Every entry
documents the body as `ApiErrorBody` (`fastapi_viewsets.error_responses`).
"""

from typing import Any

from .error_namespace import _namespaced
from .error_responses import ApiErrorBody, VALIDATION_FAILED_DETAIL


def _example(detail: str, code: str, params: dict[str, Any] | None = None, fields: dict | None = None) -> dict:
    return {"detail": detail, "detail_code": code, "detail_params": params or {}, "field_errors": fields or {}}


def _entry(description: str, examples: dict[str, tuple[str, dict]]) -> dict[str, Any]:
    return {
        "model": ApiErrorBody,
        "description": description,
        "content": {
            "application/json": {
                "examples": {name: {"summary": summary, "value": value} for name, (summary, value) in examples.items()}
            }
        },
    }


NOT_FOUND_RESPONSE = {
    "404": _entry(
        "No item has the primary key `pk`: `fastapi-viewsets.not_found`.",
        {
            "not_found": (
                "No item with this primary key",
                _example("Item with pk 42 not found", _namespaced("not_found"), {"pk": 42}),
            )
        },
    )
}
"""The `responses` entry of the endpoints that address one item by `pk`."""

BULK_NOT_FOUND_RESPONSE = {
    "404": _entry(
        "A primary key of the body has no item: `fastapi-viewsets.not_found`.",
        {
            "not_found": (
                "No item with this primary key",
                _example("Item with pk 42 not found", _namespaced("not_found"), {"pk": 42}),
            )
        },
    )
}
"""The `responses` entry of the bulk endpoints that address items by the primary keys of the body."""

_VALIDATION_FAILED_EXAMPLE = (
    "A value of the request failed validation",
    _example(
        VALIDATION_FAILED_DETAIL,
        _namespaced("validation_failed"),
        fields={
            "name": [
                {
                    "code": "pydantic.string_too_short",
                    "params": {"min_length": 3},
                    "detail": "String should have at least 3 characters",
                }
            ],
            "": [{"code": "pydantic.missing", "params": {}, "detail": "Field required"}],
        },
    ),
)

_IDEMPOTENCY_KEY_REUSED_EXAMPLE = (
    "With the Idempotency middleware: the Idempotency-Key was recorded for a different request",
    _example(
        "Idempotency key k1 was used for a different request", _namespaced("idempotency_key_reused"), {"key": "k1"}
    ),
)

_422_DESCRIPTION = (
    "The request failed validation: `fastapi-viewsets.validation_failed`, with each failure in `field_errors` "
    "under the name of its field. With the `Idempotency` middleware, an `Idempotency-Key` recorded for a "
    "different request: `fastapi-viewsets.idempotency_key_reused`."
)

_DEFAULT_DESCRIPTION = (
    "Any other error, with the same body. The package's own errors carry their codes (see the error codes "
    "guide): 400 `fastapi-viewsets.idempotency_key_invalid` (with the `Idempotency` middleware), 401 "
    "`fastapi-viewsets.session_expired` (with the `Session` middleware), 403 `fastapi-viewsets.not_authorized` "
    "(with the `Authorization` middleware), 405 `fastapi-viewsets.read_only` (a read-only collection), 429 "
    "`fastapi-viewsets.rate_limited` (with the `RateLimiter` middleware), 503 and 504 of a deferred action. "
    "An `HTTPException` of the application, or of a dependency, without a code of its own answers "
    "`fastapi-viewsets.http_error` with its status in `detail_params.status`."
)

_HTTP_ERROR_EXAMPLE = (
    "An HTTPException without a code",
    _example("Not your item", _namespaced("http_error"), {"status": 403}),
)

_RATE_LIMITED_EXAMPLE = (
    "With the RateLimiter middleware: the call was refused",
    _example("Rate limit exceeded", _namespaced("rate_limited")),
)

VIEWSET_ERROR_RESPONSES = {
    "422": _entry(
        _422_DESCRIPTION,
        {"validation_failed": _VALIDATION_FAILED_EXAMPLE, "idempotency_key_reused": _IDEMPOTENCY_KEY_REUSED_EXAMPLE},
    ),
    "500": _entry(
        "The endpoint raised an exception without a handler: `fastapi-viewsets.unhandled_error`. The body "
        "names no part of the exception; the server logs it. A deferred action that failed in its executor "
        "answers `fastapi-viewsets.action_failed`.",
        {
            "unhandled_error": (
                "An exception without a handler",
                _example("Internal Server Error", _namespaced("unhandled_error")),
            )
        },
    ),
    "default": _entry(_DEFAULT_DESCRIPTION, {"http_error": _HTTP_ERROR_EXAMPLE, "rate_limited": _RATE_LIMITED_EXAMPLE}),
}
"""The `responses` entries every endpoint of a viewset carries, under the endpoint's own entries."""

CURSOR_ERROR_RESPONSES = {
    "default": _entry(
        _DEFAULT_DESCRIPTION + " A `cursor` the list cannot continue from answers 400: "
        "`fastapi-viewsets.cursor_unreadable`, `fastapi-viewsets.cursor_missing_position`, "
        "`fastapi-viewsets.cursor_stale`, `fastapi-viewsets.cursor_missing_keys`, "
        "`fastapi-viewsets.cursor_value_mismatch`.",
        {
            "http_error": _HTTP_ERROR_EXAMPLE,
            "cursor_stale": (
                "The cursor belongs to another query",
                _example(
                    "this cursor was issued for a different ordering or filter - start from the first page",
                    _namespaced("cursor_stale"),
                ),
            ),
            "rate_limited": _RATE_LIMITED_EXAMPLE,
        },
    ),
}
"""The `responses` entry of the list endpoint; `route_viewset` keeps it on a viewset serving the cursor shape."""

SUBSCRIBE_ERROR_RESPONSES = {
    "422": _entry(
        _422_DESCRIPTION + " A malformed topic: `fastapi-viewsets.event_topic_invalid`.",
        {
            "validation_failed": _VALIDATION_FAILED_EXAMPLE,
            "event_topic_invalid": (
                "A malformed topic",
                _example(
                    "Topic @x is neither a path starting with / nor a source name with an optional key",
                    _namespaced("event_topic_invalid"),
                    {"topic": "@x"},
                ),
            ),
            "idempotency_key_reused": _IDEMPOTENCY_KEY_REUSED_EXAMPLE,
        },
    ),
}
"""The `responses` entry of the events `subscribe` endpoint."""

POLL_ERROR_RESPONSES = {
    "422": _entry(
        _422_DESCRIPTION + " More cursors than `settings.viewsets_event_max_subscriptions`: "
        "`fastapi-viewsets.event_cursors_exceeded`.",
        {
            "validation_failed": _VALIDATION_FAILED_EXAMPLE,
            "event_cursors_exceeded": (
                "More cursors than the server accepts in one poll",
                _example(
                    "A poll carries at most 256 cursors, this one carries 300",
                    _namespaced("event_cursors_exceeded"),
                    {"count": 300, "limit": 256},
                ),
            ),
            "idempotency_key_reused": _IDEMPOTENCY_KEY_REUSED_EXAMPLE,
        },
    ),
}
"""The `responses` entry of the events `poll` endpoint."""


def with_error_responses(responses: dict | None) -> dict:
    """`responses` over `VIEWSET_ERROR_RESPONSES`: an entry of `responses` replaces the entry of
    the same status."""
    merged = dict(VIEWSET_ERROR_RESPONSES)
    merged.update({str(status): entry for status, entry in (responses or {}).items()})
    return merged
