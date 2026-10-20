"""
The namespaces of the error codes this package's errors carry.

An error code is `<namespace>.<code>`: `code` is snake_case without `.`, `namespace` one or more
segments of `[a-z0-9]+(-[a-z0-9]+)*` joined by `.`, and a full code splits at its last `.`. The
codes this package declares are in `ERROR_NAMESPACE`. `ERROR_NAMESPACE_PYDANTIC` is the namespace
of pydantic's error types, which the TypeScript client's `toFieldErrors()` forwards as
`pydantic.<type>`. A code the application gives an error is used as given.
"""

from typing import Any

ERROR_NAMESPACE = "fastapi-viewsets"
"""The namespace of every error code this package declares, e.g. `fastapi-viewsets.not_found`."""

ERROR_NAMESPACE_PYDANTIC = "pydantic"
"""The namespace of pydantic's error types, e.g. `pydantic.string_too_short`."""

_PREFIX = f"{ERROR_NAMESPACE}."

_DECLARED_CODES = frozenset(
    {
        "not_found",
        "read_only",
        "session_expired",
        "not_authorized",
        "rate_limited",
        "result_timeout",
        "result_reader_stopped",
        "action_dispatch_failed",
        "action_interrupted",
        "action_failed",
        "idempotency_key_invalid",
        "idempotency_key_reused",
        "unsupported_list_shape",
        "event_topic_invalid",
        "event_topic_refused",
        "event_scope_missing",
        "event_subscriptions_exceeded",
        "event_cursors_exceeded",
        "cursor_unreadable",
        "cursor_missing_position",
        "cursor_stale",
        "cursor_missing_keys",
        "cursor_value_mismatch",
    }
)
"""Every code this package declares, without the namespace."""


def _namespaced(code: str) -> str:
    """The full code of `code`, an error code this package declares."""
    return f"{_PREFIX}{code}"


def _code_to_wire(code: Any) -> Any:
    """`code` as the JSON fields of an error carry it: without the namespace when it is a full code of
    this package's namespace, else as given."""
    if isinstance(code, str) and code.startswith(_PREFIX) and "." not in code[len(_PREFIX) :]:
        return code[len(_PREFIX) :]
    return code


def _code_from_wire(code: Any) -> Any:
    """`code` as the JSON fields of an error carry it, read back: a code this package declares, without
    a namespace, gains the namespace; any other code is read as given."""
    if isinstance(code, str) and code in _DECLARED_CODES:
        return _namespaced(code)
    return code
