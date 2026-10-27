"""
The namespaces of the error codes this package's errors carry. A code is `<namespace>.<code>`, split
at its last `.`; `code` is snake_case. Field errors of `validation_failed` carry `pydantic.<type>`. A
code the application gives an error is used as given.
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
