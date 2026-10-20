"""
The namespaces of the error codes this package's errors carry.

An error code is `<namespace>.<code>`: `code` is snake_case without `.`, `namespace` one or more
segments of `[a-z0-9]+(-[a-z0-9]+)*` joined by `.`, and a full code splits at its last `.`. The
codes this package declares are in `ERROR_NAMESPACE`. `ERROR_NAMESPACE_PYDANTIC` is the namespace
of pydantic's error types, which the TypeScript client's `toFieldErrors()` forwards as
`pydantic.<type>`. A code the application gives an error is used as given.
"""

ERROR_NAMESPACE = "fastapi-viewsets"
"""The namespace of every error code this package declares, e.g. `fastapi-viewsets.not_found`."""

ERROR_NAMESPACE_PYDANTIC = "pydantic"
"""The namespace of pydantic's error types, e.g. `pydantic.string_too_short`."""

_PREFIX = f"{ERROR_NAMESPACE}."


def _namespaced(code: str) -> str:
    """The full code of `code`, an error code this package declares."""
    return f"{_PREFIX}{code}"


def _own_code(code: str) -> str | None:
    """`code` without the namespace when it is a full code of this package's namespace, else None."""
    if code.startswith(_PREFIX) and "." not in code[len(_PREFIX) :]:
        return code[len(_PREFIX) :]
    return None
