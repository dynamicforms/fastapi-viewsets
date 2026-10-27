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

VIEWSET_ERROR_RESPONSES = {
    "422": _entry(
        "The request failed validation: `fastapi-viewsets.validation_failed`, with each failure in "
        "`field_errors` under the name of its field. A check the package makes of a valid request "
        "answers 422 too: `fastapi-viewsets.idempotency_key_reused`, `fastapi-viewsets.unsupported_list_shape`, "
        "`fastapi-viewsets.event_topic_invalid`, `fastapi-viewsets.event_cursors_exceeded`.",
        {
            "validation_failed": (
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
            ),
            "idempotency_key_reused": (
                "The Idempotency-Key was recorded for a different request",
                _example(
                    "Idempotency key k1 was used for a different request",
                    _namespaced("idempotency_key_reused"),
                    {"key": "k1"},
                ),
            ),
        },
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
    "default": _entry(
        "Any other error, with the same body. The package's own errors carry their codes (see the error "
        "codes guide): 400 `fastapi-viewsets.idempotency_key_invalid` and the cursor codes, 401 "
        "`fastapi-viewsets.session_expired`, 403 `fastapi-viewsets.not_authorized`, 405 "
        "`fastapi-viewsets.read_only`, 429 `fastapi-viewsets.rate_limited`, 503 and 504 of a deferred "
        "action. An `HTTPException` of the application, or of a dependency, without a code of its own "
        "answers `fastapi-viewsets.http_error` with its status in `detail_params.status`.",
        {
            "http_error": (
                "An HTTPException without a code",
                _example("Not your item", _namespaced("http_error"), {"status": 403}),
            ),
            "rate_limited": (
                "Command middleware refused the call",
                _example("Rate limit exceeded", _namespaced("rate_limited")),
            ),
        },
    ),
}
"""The `responses` entries every endpoint of a viewset carries, under the endpoint's own entries."""


def with_error_responses(responses: dict | None) -> dict:
    """`responses` over `VIEWSET_ERROR_RESPONSES`: an entry of `responses` replaces the entry of
    the same status."""
    merged = dict(VIEWSET_ERROR_RESPONSES)
    merged.update({str(status): entry for status, entry in (responses or {}).items()})
    return merged
