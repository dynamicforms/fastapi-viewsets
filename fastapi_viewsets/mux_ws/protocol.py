"""
The request/response envelope carried over a muxws stream.

muxws deliberately does no routing of its own - SPEC WSM-AUT-004 forbids it from looking at the
opening payload to pick a handler - so the addressing has to come from us. Rather than invent a
vocabulary, we mirror HTTP/2, which muxws already models: an `open` frame carries `headers` plus a
`payload`, exactly as HTTP/2 carries a HEADERS frame followed by DATA.

Method, path and query string therefore travel as pseudo-headers, `:`-prefixed the way HTTP/2
prefixes its own. The prefix is not decoration - it is what keeps them from colliding with real
HTTP headers, since a header field name may not contain a colon. Everything in the open frame's
headers that is *not* `:`-prefixed is passed through as an ordinary HTTP header.

    peer.open(
        payload={"title": "Kind of Blue"},          # the request body
        headers={
            ":method": "POST",
            ":path": "/music",
            "authorization": "Bearer ...",
        },
    )

The response direction is symmetric as of muxws 0.3.1, which carries `headers` on the first `data`
frame a peer sends as well as on `open` (SPEC §2.2, WSM-FRM-016). So the status is announced ahead
of the body, exactly as HTTP/2 puts `:status` in HEADERS before DATA:

    await stream.reply(body, headers={":status": 200})

and the caller reads it from `stream.reply_headers` (`replyHeaders` in TypeScript), which is final
once `reply_headers_arrived` is set.

Before 0.3.1 this had to ride in `trailers`, which only attach to a frame with `end: true`. That
was free for a unary reply but wrong for a streaming one, where the caller would have had to
consume the whole body before learning whether it was an error at all.
"""

from typing import Any
from urllib.parse import urlencode

from muxws.frames import ABSENT

METHOD_KEY = ":method"
PATH_KEY = ":path"
QUERY_KEY = ":query"
STATUS_KEY = ":status"


class EnvelopeError(ValueError):
    """The open frame's headers are not a well-formed viewset command."""


def is_command(headers: dict[str, Any] | None) -> bool:
    """
    Whether this stream addresses a viewset at all. Streams that carry neither pseudo-header are
    somebody else's protocol sharing the same peer, and must be left alone - see
    `process_command`'s return value.
    """
    return bool(headers) and METHOD_KEY in headers and PATH_KEY in headers


def parse_request(headers: dict[str, Any] | None) -> tuple[str, str, bytes, dict[str, str]]:
    """
    Splits an open frame's headers into (method, path, query_string, http_headers).

    `:query` accepts either a pre-encoded string or a mapping. A mapping is the friendlier thing
    to write by hand, but it cannot express a repeated key (`?genre=jazz&genre=blues`), which
    FastAPI happily binds to a `list[str]` parameter - so a list value is expanded into repeats
    rather than being encoded as one comma-joined value.
    """
    if not is_command(headers):
        raise EnvelopeError(f"stream headers carry no {METHOD_KEY}/{PATH_KEY} - not a viewset command")

    method = headers[METHOD_KEY]
    path = headers[PATH_KEY]
    if not isinstance(method, str) or not isinstance(path, str):
        raise EnvelopeError(f"{METHOD_KEY} and {PATH_KEY} must both be strings")
    if not path.startswith("/"):
        raise EnvelopeError(f"{PATH_KEY} must be absolute, got {path!r}")

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
        raise EnvelopeError(f"{QUERY_KEY} must be a string or a mapping, got {type(query).__name__}")

    http_headers = {str(key).lower(): str(value) for key, value in headers.items() if not str(key).startswith(":")}
    return method.upper(), path, query_string, http_headers


def merge_headers(base: dict[str, str] | None, override: dict[str, str]) -> dict[str, str]:
    """
    Per-call headers win over the connection's own.

    The WebSocket handshake usually already carries whatever identifies the session, and for most
    calls that is the whole story. But a single connection can outlive a token, and a caller may
    legitimately want to act under different credentials for one call without tearing the socket
    down - so anything the open frame states replaces the handshake's value for that key.
    """
    merged = {key.lower(): value for key, value in (base or {}).items()}
    merged.update(override)
    return merged


def build_response_meta(status: int, headers: dict[str, str]) -> dict[str, Any]:
    """The leading headers that announce a response, status first."""
    meta: dict[str, Any] = {STATUS_KEY: status}
    meta.update(headers)
    return meta


def body_to_payload(body: Any) -> Any:
    """`ABSENT` is muxws' "no payload at all", which is a different frame from a null payload."""
    return ABSENT if body is None else body
