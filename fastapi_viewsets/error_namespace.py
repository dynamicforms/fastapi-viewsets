"""
The namespaces of the error codes this package's errors carry.

An error code is `<namespace>.<code>`: `code` is snake_case without `.`, `namespace` one or more
segments of `[a-z0-9]+(-[a-z0-9]+)*` joined by `.`, and a full code splits at its last `.`. The
codes this package declares are in `ERROR_NAMESPACE`. `ERROR_NAMESPACE_PYDANTIC` is the namespace
of pydantic's error types, which the field errors of a `fastapi-viewsets.validation_failed` body carry
as `pydantic.<type>`, and of `pydantic.custom_error`, the field error of a type pydantic does not
declare. A code the application gives an error is used as given.
"""

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
        "validation_failed",
        "http_error",
        "unhandled_error",
        "command_invalid",
        "pk_assigned_by_server",
    }
)
"""Every code this package declares, without the namespace; each is built with `_namespaced()`."""


def _namespaced(code: str) -> str:
    """The full code of `code`, an error code this package declares."""
    return f"{_PREFIX}{code}"
