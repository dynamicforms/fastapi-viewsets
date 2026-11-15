"""
The namespaces of this package's error codes. A code is `<namespace>.<code>`, split at its last `.`;
`code` is snake_case. The code of a `validation_failed` field error is one of this package's field
error codes (`FIELD_ERROR_PARAMS`), `pydantic.<type>` or `django.<code>`. An application's namespaced
code is returned unchanged.
"""

ERROR_NAMESPACE = "fastapi-viewsets"
"""The namespace of every error code this package declares, e.g. `fastapi-viewsets.not_found`."""

ERROR_NAMESPACE_PYDANTIC = "pydantic"
"""
The namespace of pydantic's error types in field errors that have no field error code, e.g.
`pydantic.json_invalid`, and of `pydantic.custom_error`.
"""

ERROR_NAMESPACE_DJANGO = "django"
"""
The namespace of Django's validation codes in field errors that have no field error code, e.g.
`django.unique_for_date`.
"""

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
        "required",
        "min_length",
        "max_length",
        "min_value",
        "min_value_exclusive",
        "max_value",
        "max_value_exclusive",
        "multiple_of",
        "max_digits",
        "max_decimal_places",
        "max_whole_digits",
        "pattern",
        "in_allowed_values",
        "invalid_choice",
        "invalid_integer",
        "invalid_number",
        "invalid_boolean",
        "invalid_date",
        "invalid_time",
        "invalid_datetime",
        "invalid_duration",
        "invalid_url",
        "invalid_uuid",
        "past",
        "future",
        "invalid_type",
        "unique",
        "invalid",
    }
)
"""Every code this package declares, without the namespace; each is built with `_namespaced()`."""


def _namespaced(code: str) -> str:
    """The full code of `code`, an error code this package declares."""
    return f"{_PREFIX}{code}"
