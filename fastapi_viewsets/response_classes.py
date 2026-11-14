"""
The OpenAPI `responses` entries of the error responses of a viewset's endpoints, and `error_entry()` /
`merge_responses()`, which build and merge such entries. Every entry documents the body as `ApiErrorBody`
(`fastapi_viewsets.error_responses`).
"""

import copy

from typing import Any

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError

from .cursor import CursorError
from .error_namespace import _namespaced
from .error_responses import ApiErrorBody, error_body
from .exceptions import (
    CursorRequestError,
    EventCursorsExceededError,
    EventTopicInvalidError,
    IdempotencyKeyReusedError,
    NotFoundError,
    RateLimitedError,
    UnhandledError,
)

_JSON = "application/json"
_ERROR_BODY_REF = {"$ref": "#/components/schemas/ApiErrorBody"}
_DESCRIPTION_SEPARATOR = "\n\n"


def error_entry(
    status: int | str, description: str, *errors: BaseException, as_ref: bool = False, **named_errors: BaseException
) -> dict[str, dict[str, Any]]:
    """
    `{str(status): entry}`, an OpenAPI `responses` entry whose body is `ApiErrorBody`, with one example per
    exception, its body built with `error_body()`. A positional exception's example is named after the last
    segment of its code and summarised by its detail; a keyword exception's example is named and summarised
    by its keyword. The body is `model: ApiErrorBody` (the form of route declarations and `@endpoint_docs`);
    with `as_ref` it is `content.application/json.schema` referring to `#/components/schemas/ApiErrorBody`,
    the form for editing a generated schema. Two examples of one name raise `ValueError`.
    """
    examples: dict[str, dict[str, Any]] = {}

    def add(name: str, summary: str, value: dict[str, Any]) -> None:
        if name in examples:
            raise ValueError(f"error_entry({status!r}) has two examples named {name!r}; name one by keyword")
        examples[name] = {"summary": summary, "value": value}

    for error in errors:
        body = error_body(error)
        add(body["detail_code"].rsplit(".", 1)[-1], body["detail"], body)
    for name, error in named_errors.items():
        add(name, name, error_body(error))

    media: dict[str, Any] = {"schema": dict(_ERROR_BODY_REF)} if as_ref else {}
    if examples:
        media["examples"] = examples
    entry: dict[str, Any] = {} if as_ref else {"model": ApiErrorBody}
    entry["description"] = description
    entry["content"] = {_JSON: media}
    return {str(status): entry}


def _merged_description(first: str | None, second: str | None) -> str | None:
    """Both descriptions as paragraphs, each paragraph once."""
    paragraphs: list[str] = []
    for text in (first, second):
        for paragraph in (text or "").split(_DESCRIPTION_SEPARATOR):
            if paragraph.strip() and paragraph not in paragraphs:
                paragraphs.append(paragraph)
    return _DESCRIPTION_SEPARATOR.join(paragraphs) if paragraphs else None


def _add_lone_example(examples: dict[str, Any], example: dict[str, Any]) -> None:
    """Adds `example` as `example` (`example_2`, ... when taken) unless an equal example is there."""
    if example in examples.values():
        return
    name, number = "example", 1
    while name in examples:
        number += 1
        name = f"example_{number}"
    examples[name] = example


def _merged_media(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    merged = {**first, **second}
    if any(key in media for media in (first, second) for key in ("example", "examples")):
        examples: dict[str, Any] = {}
        for media in (first, second):
            examples.update(media.get("examples") or {})
            if "example" in media:
                _add_lone_example(examples, {"value": media["example"]})
        merged.pop("example", None)
        merged["examples"] = examples
    return merged


def _merged_entry(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    merged = {**first, **second}
    description = _merged_description(first.get("description"), second.get("description"))
    if description is not None:
        merged["description"] = description
    if "content" in first or "content" in second:
        content = dict(first.get("content") or {})
        for media_type, media in (second.get("content") or {}).items():
            content[media_type] = _merged_media(content[media_type], media) if media_type in content else media
        merged["content"] = content
    if "headers" in first or "headers" in second:
        merged["headers"] = {**(first.get("headers") or {}), **(second.get("headers") or {})}
    return merged


def merge_responses(*responses: dict[Any, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """
    OpenAPI `responses` merged by status (keys as strings), in the `model` or the `$ref` form: descriptions
    joined as paragraphs, each once; `content` combined per media type, with `examples` merged by name (a
    later example replaces one of the same name; a lone `example` becomes a named one); `headers` merged by
    name; any other key of a later entry replaces the earlier one's. The arguments are not modified.
    """
    merged: dict[str, dict[str, Any]] = {}
    for response in responses:
        for status, entry in (response or {}).items():
            key = str(status)
            entry = copy.deepcopy(entry)
            merged[key] = _merged_entry(merged[key], entry) if key in merged else entry
    return merged


NOT_FOUND_RESPONSE = error_entry(
    404, "No item has the primary key `pk`: `fastapi-viewsets.not_found`.", NotFoundError(42)
)
"""The `responses` entry of the endpoints that address one item by `pk`."""

BULK_NOT_FOUND_RESPONSE = error_entry(
    404, "A primary key of the body has no item: `fastapi-viewsets.not_found`.", NotFoundError(42)
)
"""The `responses` entry of the bulk endpoints that address items by the primary keys of the body."""

_VALIDATION_FAILED = RequestValidationError(
    [
        {
            "type": "string_too_short",
            "loc": ("body", "name"),
            "msg": "String should have at least 3 characters",
            "ctx": {"min_length": 3},
        },
        {"type": "missing", "loc": ("body",), "msg": "Field required"},
    ]
)

_IDEMPOTENCY_KEY_REUSED = IdempotencyKeyReusedError("k1")

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

_HTTP_ERROR = HTTPException(403, "Not your item")

VIEWSET_ERROR_RESPONSES = merge_responses(
    error_entry(422, _422_DESCRIPTION, _VALIDATION_FAILED, _IDEMPOTENCY_KEY_REUSED),
    error_entry(
        500,
        "The endpoint raised an exception without a handler: `fastapi-viewsets.unhandled_error`. The body "
        "names no part of the exception; the server logs it. A deferred action that failed in its executor "
        "answers `fastapi-viewsets.action_failed`.",
        UnhandledError(),
    ),
    error_entry("default", _DEFAULT_DESCRIPTION, _HTTP_ERROR, RateLimitedError()),
)
"""The `responses` entries every endpoint of a viewset carries, under the endpoint's own entries."""

CURSOR_ERROR_RESPONSES = error_entry(
    "default",
    _DEFAULT_DESCRIPTION + " A `cursor` the list cannot continue from answers 400: "
    "`fastapi-viewsets.cursor_unreadable`, `fastapi-viewsets.cursor_missing_position`, "
    "`fastapi-viewsets.cursor_stale`, `fastapi-viewsets.cursor_missing_keys`, "
    "`fastapi-viewsets.cursor_value_mismatch`.",
    _HTTP_ERROR,
    CursorRequestError(
        CursorError(
            "this cursor was issued for a different ordering or filter - start from the first page",
            _namespaced("cursor_stale"),
        )
    ),
    RateLimitedError(),
)
"""The `responses` entry of the list endpoint; `route_viewset` keeps it on a viewset serving the cursor shape."""

SUBSCRIBE_ERROR_RESPONSES = error_entry(
    422,
    _422_DESCRIPTION + " A malformed topic: `fastapi-viewsets.event_topic_invalid`.",
    _VALIDATION_FAILED,
    EventTopicInvalidError("@x"),
    _IDEMPOTENCY_KEY_REUSED,
)
"""The `responses` entry of the events `subscribe` endpoint."""

POLL_ERROR_RESPONSES = error_entry(
    422,
    _422_DESCRIPTION + " More cursors than `settings.viewsets_event_max_subscriptions`: "
    "`fastapi-viewsets.event_cursors_exceeded`.",
    _VALIDATION_FAILED,
    EventCursorsExceededError(300, 256),
    _IDEMPOTENCY_KEY_REUSED,
)
"""The `responses` entry of the events `poll` endpoint."""


def with_error_responses(responses: dict | None) -> dict:
    """`responses` over `VIEWSET_ERROR_RESPONSES`: an entry of `responses` replaces the entry of
    the same status."""
    merged = dict(VIEWSET_ERROR_RESPONSES)
    merged.update({str(status): entry for status, entry in (responses or {}).items()})
    return merged
