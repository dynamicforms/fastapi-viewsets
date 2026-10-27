"""
The HTTP/2-style request/response envelope carried over a muxws stream.

The open frame's headers carry `:method`, `:path` and `:query` pseudo-headers (other headers pass through
as HTTP headers) and its payload is the body. The reply announces `:status` in the headers of its first
data frame, ahead of the body; trailers would arrive only after a streamed body.
"""

from typing import Any
from urllib.parse import urlencode

METHOD_KEY = ":method"
PATH_KEY = ":path"
QUERY_KEY = ":query"
STATUS_KEY = ":status"


class EnvelopeError(ValueError):
    """
    The open frame's headers are not a well-formed viewset command. `reason`: `address_missing`,
    `address_type`, `path_relative` or `query_type`.
    """

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


def is_command(headers: dict[str, Any] | None) -> bool:
    """Whether the stream addresses a viewset; other streams belong to another protocol on the same peer."""
    return bool(headers) and METHOD_KEY in headers and PATH_KEY in headers


def parse_request(headers: dict[str, Any] | None) -> tuple[str, str, bytes, dict[str, str]]:
    """
    Splits an open frame's headers into (method, path, query_string, http_headers). `:query` is an
    encoded string or a mapping, where a list value becomes a repeated key. Raises EnvelopeError.
    """
    if not is_command(headers):
        raise EnvelopeError(
            f"stream headers carry no {METHOD_KEY}/{PATH_KEY} - not a viewset command", "address_missing"
        )

    method = headers[METHOD_KEY]
    path = headers[PATH_KEY]
    if not isinstance(method, str) or not isinstance(path, str):
        raise EnvelopeError(f"{METHOD_KEY} and {PATH_KEY} must both be strings", "address_type")
    if not path.startswith("/"):
        raise EnvelopeError(f"{PATH_KEY} must be absolute, got {path!r}", "path_relative")

    query = headers.get(QUERY_KEY) or ""
    if isinstance(query, str):
        query_string = query.lstrip("?").encode()
    elif isinstance(query, dict):
        pairs: list[tuple[str, str]] = []
        for key, value in query.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                pairs.extend((key, str(item)) for item in value)
            else:
                pairs.append((key, str(value)))
        query_string = urlencode(pairs).encode()
    else:
        raise EnvelopeError(f"{QUERY_KEY} must be a string or a mapping, got {type(query).__name__}", "query_type")

    http_headers = {str(key).lower(): str(value) for key, value in headers.items() if not str(key).startswith(":")}
    return method.upper(), path, query_string, http_headers


def merge_headers(base: dict[str, str] | None, override: dict[str, str]) -> dict[str, str]:
    """Per-call headers override the handshake's (e.g. other credentials for one call); keys lower-cased."""
    merged = {key.lower(): value for key, value in (base or {}).items()}
    merged.update(override)
    return merged


def build_response_meta(status: int, headers: dict[str, str]) -> dict[str, Any]:
    """The leading headers that announce a response, status first."""
    meta: dict[str, Any] = {STATUS_KEY: status}
    meta.update(headers)
    return meta
