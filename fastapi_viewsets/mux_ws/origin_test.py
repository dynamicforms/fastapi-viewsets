import pytest

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from .origin import origin_allowed


def connection(**headers: str) -> Request:
    raw = [(name.replace("_", "-").encode(), value.encode()) for name, value in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/ws", "headers": raw})


def test_a_handshake_without_origin_is_allowed():
    assert origin_allowed(connection(host="app.example"))


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "app.example",
        "ftp://app.example",
        "https://app.example/path",
        "https://app.example?x=1",
        "https://user@app.example",
        "https://app.example:99999",
    ],
)
def test_an_origin_that_is_not_scheme_host_port_is_refused(origin):
    assert not origin_allowed(connection(origin=origin, host="app.example"))


@pytest.mark.parametrize(
    ("origin", "host", "allowed"),
    [
        ("https://app.example", "app.example", True),
        ("https://APP.example/", "app.example", True),
        ("https://app.example", "app.example:443", True),
        ("http://app.example", "app.example:80", True),
        ("http://app.example:8000", "app.example:8000", True),
        ("http://app.example", "app.example:8000", False),
        ("https://evil.example", "app.example", False),
        ("https://app.example", "app.example:8443", False),
        ("http://localhost:5173", "localhost:5173", True),
    ],
)
def test_without_a_list_the_origin_matches_the_request_host(origin, host, allowed):
    assert origin_allowed(connection(origin=origin, host=host)) is allowed


def test_the_scheme_is_not_compared_with_the_request_host():
    assert origin_allowed(connection(origin="https://app.example:8000", host="app.example:8000"))


def test_x_forwarded_host_names_the_request_host():
    request = connection(origin="https://app.example", host="backend:8000", x_forwarded_host="app.example, proxy")
    assert origin_allowed(request)
    assert not origin_allowed(connection(origin="https://app.example", host="backend:8000"))


def test_without_a_host_a_browser_origin_is_refused():
    assert not origin_allowed(connection(origin="https://app.example"))


def test_more_than_one_origin_header_is_refused():
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"origin", b"https://app.example"),
                (b"origin", b"https://app.example"),
                (b"host", b"app.example"),
            ],
        }
    )
    assert not origin_allowed(request)


def test_with_a_list_the_origin_matches_one_of_its_entries():
    allowed = ["https://app.example", "http://localhost:5173/"]
    assert origin_allowed(connection(origin="https://app.example:443", host="backend"), allowed)
    assert origin_allowed(connection(origin="http://localhost:5173", host="backend"), allowed)
    assert not origin_allowed(connection(origin="http://app.example", host="app.example"), allowed)
    assert origin_allowed(connection(host="backend"), allowed)


def test_an_entry_that_is_not_an_origin_raises():
    with pytest.raises(ValueError, match="is not an origin"):
        origin_allowed(connection(origin="https://app.example"), ["app.example"])


def test_an_endpoint_closes_a_refused_handshake_before_accept():
    app = FastAPI()

    @app.websocket("/ws")
    async def endpoint(websocket: WebSocket) -> None:
        if not origin_allowed(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await websocket.send_text("hello")
        await websocket.close()

    client = TestClient(app)
    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as websocket:
        assert websocket.receive_text() == "hello"
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}):
            pass
    assert refused.value.code == 1008
