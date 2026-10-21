import logging

from typing import Any

import pytest

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from muxws.frames import ABSENT
from pydantic import BaseModel, Field

from ..collection_viewset import CollectionViewSet
from ..conf import settings
from ..context import Context
from ..decorators import route_viewset
from ..mixins import BulkViewSetMixin
from .registry import is_registered, registered_viewsets, reset_registry, schema_for
from .server import process_command
from .transports import transports


class Track(BaseModel):
    id: int = Field(json_schema_extra={"autoinc_int": True})
    title: str


class FakeStream:
    """
    Stands in for a muxws Stream. Only `headers` and `reply` are exercised - `process_command`
    answers every command with a single unary reply and touches nothing else.
    """

    def __init__(self, headers: dict[str, Any] | None):
        self.headers = headers
        self.payload: Any = None
        self.reply_headers: dict[str, Any] = {}
        self.trailers: dict[str, Any] | None = None
        self.replies = 0

    async def reply(
        self,
        payload: Any,
        *,
        headers: dict[str, Any] | None = None,
        trailers: dict[str, Any] | None = None,
    ) -> None:
        self.payload = payload
        self.reply_headers = headers or {}
        self.trailers = trailers
        self.replies += 1

    @property
    def status(self) -> int | None:
        """Read from the answering side's leading headers, as a real caller would."""
        return self.reply_headers.get(":status")


def make_viewset(register_muxws: bool | None = None) -> type:
    database = {1: Track(id=1, title="Kind of Blue"), 2: Track(id=2, title="Blue Train")}
    router = APIRouter()

    @route_viewset(router, base_path="/tracks", pk_field_name="id", register_muxws=register_muxws)
    class TrackViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=database, pk_field="id")

    return TrackViewSet


def app_of(cls: type, prefix: str = "") -> FastAPI:
    """The application a viewset's router is included in, as `route_viewset`'s caller would build it."""
    app = FastAPI()
    app.include_router(cls.__viewset_metadata__["router"], prefix=prefix)
    return app


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry()
    yield
    reset_registry()


@pytest.mark.asyncio
async def test_non_command_streams_are_left_alone():
    """
    A peer may carry the application's own protocol alongside viewset traffic, so a stream we do
    not recognise must come back unhandled and unanswered rather than being refused.
    """
    cls = make_viewset()
    stream = FakeStream({"action": "quote", "symbol": "ACME"})
    assert await process_command({"any": "payload"}, stream, app=app_of(cls)) is False
    assert stream.replies == 0


@pytest.mark.asyncio
async def test_list_returns_the_collection():
    cls = make_viewset()
    stream = FakeStream({":method": "GET", ":path": "/tracks"})
    assert await process_command(ABSENT, stream, app=app_of(cls)) is True
    assert stream.status == 200
    assert stream.payload == [
        {"id": 1, "title": "Kind of Blue"},
        {"id": 2, "title": "Blue Train"},
    ]


@pytest.mark.asyncio
async def test_retrieve_binds_the_path_parameter():
    cls = make_viewset()
    stream = FakeStream({":method": "GET", ":path": "/tracks/2"})
    await process_command(ABSENT, stream, app=app_of(cls))
    assert stream.status == 200
    assert stream.payload == {"id": 2, "title": "Blue Train"}


@pytest.mark.asyncio
async def test_create_binds_the_payload_as_the_request_body():
    cls = make_viewset()
    stream = FakeStream({":method": "POST", ":path": "/tracks"})
    await process_command({"title": "Giant Steps"}, stream, app=app_of(cls))
    assert stream.status == 200
    assert stream.payload["title"] == "Giant Steps"


@pytest.mark.asyncio
async def test_a_missing_record_answers_with_a_status_not_a_reset():
    """
    404 is an outcome, not a transport failure. Resetting the stream would tell the caller the
    connection misbehaved, when in fact the server answered perfectly well.
    """
    cls = make_viewset()
    stream = FakeStream({":method": "GET", ":path": "/tracks/999"})
    assert await process_command(ABSENT, stream, app=app_of(cls)) is True
    assert stream.status == 404


@pytest.mark.asyncio
async def test_validation_errors_come_back_as_422_with_the_body_rest_answers():
    cls = make_viewset()
    app = app_of(cls)
    stream = FakeStream({":method": "POST", ":path": "/tracks"})
    await process_command({"title": 12345}, stream, app=app)
    rest = TestClient(app).post("/tracks", json={"title": 12345})
    assert stream.status == rest.status_code == 422
    assert stream.payload == rest.json()
    assert stream.payload == {
        "detail": "The request failed validation",
        "detail_code": "fastapi-viewsets.validation_failed",
        "detail_params": {},
        "field_errors": {
            "title": [{"code": "pydantic.string_type", "params": {}, "detail": "Input should be a valid string"}]
        },
    }


@pytest.mark.asyncio
async def test_an_unregistered_path_answers_404():
    cls = make_viewset()
    stream = FakeStream({":method": "GET", ":path": "/nonexistent"})
    assert await process_command(ABSENT, stream, app=app_of(cls)) is True
    assert stream.status == 404
    assert stream.payload == {"detail": "Not Found"}


@pytest.mark.asyncio
async def test_an_unregistered_path_answers_http_error_once_the_app_installs_the_handlers():
    from ..error_responses import install_error_handlers

    app = app_of(make_viewset())
    install_error_handlers(app)
    stream = FakeStream({":method": "GET", ":path": "/nonexistent"})
    await process_command(ABSENT, stream, app=app)
    assert stream.status == 404
    assert stream.payload == {
        "detail": "Not Found",
        "detail_code": "fastapi-viewsets.http_error",
        "detail_params": {"status": 404},
        "field_errors": {},
    }


@pytest.mark.asyncio
async def test_query_parameters_reach_the_endpoint():
    cls = make_viewset()
    stream = FakeStream({":method": "GET", ":path": "/tracks", ":query": {"sort": "id:desc"}})
    await process_command(ABSENT, stream, app=app_of(cls))
    assert stream.status == 200
    assert [record["id"] for record in stream.payload] == [2, 1]


@pytest.mark.asyncio
async def test_schema_reports_the_muxws_endpoint_set():
    cls = make_viewset()
    stream = FakeStream({":method": "GET", ":path": "/tracks/schema"})
    await process_command(ABSENT, stream, app=app_of(cls))
    assert stream.status == 200
    assert "/tracks" in stream.payload["paths"]


@pytest.mark.asyncio
async def test_schema_on_each_transport_lists_itself_under_the_viewset_base_path():
    """The document's paths omit the include_router prefix; its own /schema path names the base."""
    cls = make_viewset()
    app = app_of(cls, prefix="/api/v1")
    stream = FakeStream({":method": "GET", ":path": "/api/v1/tracks/schema"})
    await process_command(ABSENT, stream, app=app)
    rest_paths = TestClient(app).get("/api/v1/tracks/schema").json()["paths"]
    for paths in (rest_paths, stream.payload["paths"]):
        assert "/tracks/schema" in paths
        assert "/tracks/{pk}" in paths

@pytest.mark.asyncio
async def test_headers_stated_on_the_stream_override_the_connection():
    captured: dict[str, str] = {}

    async def capture(request, _viewset) -> dict[str, Any]:
        captured.update(request.headers)
        return {}

    settings.viewsets_context_processors = [capture]
    try:
        database = {1: Track(id=1, title="Kind of Blue")}
        router = APIRouter()

        @route_viewset(router, base_path="/ctx", pk_field_name="id")
        class CtxViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
            def __init__(self):
                super().__init__(container=database, pk_field="id")

            async def perform_list(self, context: Context) -> list[Track]:
                return await super().perform_list(context)

        stream = FakeStream(
            {
                ":method": "GET",
                ":path": "/ctx",
                "authorization": "Bearer per-call",
            }
        )
        await process_command(
            ABSENT,
            stream,
            base_headers={"authorization": "Bearer handshake", "x-trace": "abc"},
            app=app_of(CtxViewSet),
        )
        assert stream.status == 200
        assert captured["authorization"] == "Bearer per-call"
        assert captured["x-trace"] == "abc"
    finally:
        settings.viewsets_context_processors = []


class FakeConnection:
    """Stands in for the WebSocket: `process_command` reads its `headers` and `scope`."""

    def __init__(
        self,
        app: FastAPI,
        client: tuple[str, int] = ("203.0.113.7", 51234),
        headers: dict[str, str] | None = None,
        state: dict[str, Any] | None = None,
    ):
        self.headers: dict[str, str] = headers or {}
        self.scope = {"app": app, "client": client, "server": ("10.0.0.1", 8000), "state": state or {}}


@pytest.mark.asyncio
async def test_the_request_carries_the_connection_address():
    captured: dict[str, Any] = {}

    async def capture(request, _viewset) -> dict[str, Any]:
        captured["client"] = (request.client.host, request.client.port)
        return {}

    settings.viewsets_context_processors = [capture]
    try:
        cls = make_viewset()
        stream = FakeStream({":method": "GET", ":path": "/tracks"})
        await process_command(ABSENT, stream, connection=FakeConnection(app_of(cls), ("203.0.113.7", 51234)))
        assert stream.status == 200
        assert captured["client"] == ("203.0.113.7", 51234)
    finally:
        settings.viewsets_context_processors = []


@pytest.mark.asyncio
async def test_rate_limiter_counts_each_connection_address_separately():
    from ..middleware.rate_limiter import RateLimiter

    settings.viewsets_command_middleware = [RateLimiter(default_limit=1)]
    try:
        cls = make_viewset()
        first = FakeStream({":method": "GET", ":path": "/tracks"})
        other = FakeStream({":method": "GET", ":path": "/tracks"})
        again = FakeStream({":method": "GET", ":path": "/tracks"})
        app = app_of(cls)
        await process_command(ABSENT, first, connection=FakeConnection(app, ("203.0.113.7", 1)))
        await process_command(ABSENT, other, connection=FakeConnection(app, ("203.0.113.8", 1)))
        await process_command(ABSENT, again, connection=FakeConnection(app, ("203.0.113.7", 2)))
        assert (first.status, other.status, again.status) == (200, 200, 429)
    finally:
        settings.viewsets_command_middleware = []


@pytest.mark.asyncio
async def test_register_muxws_false_keeps_the_viewset_off_the_transport():
    cls = make_viewset(register_muxws=False)
    assert not is_registered(cls)
    stream = FakeStream({":method": "GET", ":path": "/tracks"})
    await process_command(ABSENT, stream, app=app_of(cls))
    assert stream.status == 404


@pytest.mark.asyncio
async def test_the_global_setting_decides_when_nothing_else_does():
    settings.viewsets_register_muxws = False
    try:
        cls = make_viewset()
        assert not is_registered(cls)
    finally:
        settings.viewsets_register_muxws = True


@pytest.mark.asyncio
async def test_a_viewset_level_true_overrides_a_global_false():
    settings.viewsets_register_muxws = False
    try:
        cls = make_viewset(register_muxws=True)
        assert is_registered(cls)
    finally:
        settings.viewsets_register_muxws = True


@pytest.mark.asyncio
async def test_transports_can_publish_an_endpoint_on_muxws_only():
    database = {1: Track(id=1, title="Kind of Blue")}
    rest_router = APIRouter()

    @route_viewset(rest_router, base_path="/split", pk_field_name="id")
    class SplitViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        __router = APIRouter()

        @transports(rest=False)
        @__router.get("live", tags=["Split"])
        async def live(self, context: Context) -> int:
            return len(await self.perform_list(context))

        def __init__(self):
            super().__init__(container=database, pk_field="id")

    app = app_of(SplitViewSet)
    # Over REST the route is not there: /split/live reaches /split/{pk}, which refuses "live" as a key.
    assert TestClient(app).get("/split/live").status_code == 422
    assert "/split/live" not in app.openapi()["paths"]

    stream = FakeStream({":method": "GET", ":path": "/split/live"})
    await process_command(ABSENT, stream, app=app)
    assert stream.status == 200
    assert stream.payload == 1


@pytest.mark.asyncio
async def test_transports_can_keep_an_endpoint_off_muxws():
    database = {1: Track(id=1, title="Kind of Blue")}
    rest_router = APIRouter()

    @route_viewset(rest_router, base_path="/restonly", pk_field_name="id")
    class RestOnlyViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        __router = APIRouter()

        @transports(muxws=False)
        @__router.get("secret", tags=["RestOnly"])
        async def secret(self, context: Context) -> int:  # noqa: ARG002 - context is the hook, unused here
            return 42

        def __init__(self):
            super().__init__(container=database, pk_field="id")

    app = app_of(RestOnlyViewSet)
    assert TestClient(app).get("/restonly/secret").json() == 42

    # Over muxws the route is not there: /restonly/secret reaches /restonly/{pk}, which refuses
    # "secret" as a key.
    stream = FakeStream({":method": "GET", ":path": "/restonly/secret"})
    await process_command(ABSENT, stream, app=app)
    assert stream.status == 422


def test_transports_refuses_an_unreachable_endpoint():
    with pytest.raises(ValueError, match="unreachable"):
        transports(rest=False, muxws=False)


def test_re_registering_the_same_class_replaces_rather_than_duplicates():
    """Re-importing a module re-runs the decorator; that must not shadow the first registration."""
    database = {1: Track(id=1, title="Kind of Blue")}

    class TwiceViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=database, pk_field="id")

    for _ in range(2):
        route_viewset(APIRouter(), base_path="/twice", pk_field_name="id")(TwiceViewSet)

    assert list(registered_viewsets().values()).count("/twice") == 1


def test_registry_reports_registered_viewsets():
    cls = make_viewset()
    assert registered_viewsets()[cls] == "/tracks"
    assert schema_for("/tracks") is not None
    assert schema_for("/nope") is None


@pytest.mark.asyncio
async def test_register_rest_false_makes_a_viewset_muxws_only():
    database = {1: Track(id=1, title="Kind of Blue")}
    rest_router = APIRouter()

    @route_viewset(rest_router, base_path="/wsonly", pk_field_name="id", register_rest=False)
    class WsOnlyViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=database, pk_field="id")

    assert is_registered(WsOnlyViewSet)
    app = app_of(WsOnlyViewSet)
    assert TestClient(app).get("/wsonly").status_code == 404
    assert app.openapi()["paths"] == {}

    stream = FakeStream({":method": "GET", ":path": "/wsonly"})
    await process_command(ABSENT, stream, app=app)
    assert stream.status == 200


@pytest.mark.asyncio
async def test_an_endpoint_may_still_opt_back_into_rest():
    """Endpoint beats viewset, the same way round as it does for muxws."""
    database = {1: Track(id=1, title="Kind of Blue")}
    rest_router = APIRouter()

    @route_viewset(rest_router, base_path="/mostly-ws", pk_field_name="id", register_rest=False)
    class MostlyWsViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        __router = APIRouter()

        @transports(rest=True)
        @__router.get("health", tags=["MostlyWs"])
        async def health(self, context: Context) -> int:  # noqa: ARG002 - context is the hook
            return 1

        def __init__(self):
            super().__init__(container=database, pk_field="id")

    assert list(app_of(MostlyWsViewSet).openapi()["paths"]) == ["/mostly-ws/health"]


def test_two_viewsets_on_one_base_path_warn():
    """
    Neither FastAPI nor this library would otherwise say anything, and the later viewset's
    endpoints are simply unreachable.
    """
    make_viewset()
    database = {1: Track(id=1, title="Kind of Blue")}
    router = APIRouter()

    class ImpostorViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=database, pk_field="id")

    with pytest.warns(UserWarning, match="unreachable"):
        route_viewset(router, base_path="/tracks", pk_field_name="id")(ImpostorViewSet)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "reason", "detail"),
    [
        ({":method": "GET", ":path": "tracks"}, "path_relative", ":path must be an absolute path"),
        ({":method": "GET", ":path": "/tracks", ":query": 42}, "query_type", ":query must be a string or a mapping"),
        ({":method": 1, ":path": "/tracks"}, "address_type", ":method and :path must both be strings"),
    ],
)
async def test_a_malformed_envelope_is_answered_with_a_400_command_invalid(headers, reason, detail):
    cls = make_viewset()
    stream = FakeStream(headers)
    assert await process_command(ABSENT, stream, app=app_of(cls)) is True
    assert stream.status == 400
    assert stream.payload == {
        "detail": detail,
        "detail_code": "fastapi-viewsets.command_invalid",
        "detail_params": {"reason": reason},
        "field_errors": {},
    }


# A command goes through the application, as an HTTP request to the same path does.


@pytest.mark.asyncio
async def test_a_command_without_connection_or_app_raises():
    make_viewset()
    with pytest.raises(ValueError, match="connection= .* or app="):
        await process_command(ABSENT, FakeStream({":method": "GET", ":path": "/tracks"}))


@pytest.mark.asyncio
async def test_the_command_goes_through_the_app_serving_the_connection():
    cls = make_viewset()
    app = app_of(cls)
    seen: list[str] = []

    @app.middleware("http")
    async def mark(request, call_next):
        seen.append(request.url.path)
        response = await call_next(request)
        response.headers["x-app-middleware"] = "yes"
        return response

    stream = FakeStream({":method": "GET", ":path": "/tracks"})
    await process_command(ABSENT, stream, connection=FakeConnection(app))
    assert stream.status == 200
    assert seen == ["/tracks"]
    assert stream.reply_headers["x-app-middleware"] == "yes"


@pytest.mark.asyncio
async def test_a_viewset_error_carries_the_body_rest_answers():
    cls = make_viewset()
    app = app_of(cls)

    rest = TestClient(app).get("/tracks/999")
    stream = FakeStream({":method": "GET", ":path": "/tracks/999"})
    await process_command(ABSENT, stream, app=app)
    assert stream.status == rest.status_code == 404
    assert stream.payload == rest.json()
    assert stream.payload["detail_code"] == "fastapi-viewsets.not_found"


@pytest.mark.asyncio
async def test_the_apps_500_handler_answers_an_unhandled_error():
    from fastapi.responses import JSONResponse

    router = APIRouter()

    @route_viewset(router, base_path="/broken", pk_field_name="id")
    class BrokenViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container={}, pk_field="id")

        async def perform_list(self, context: Context) -> list[Track]:  # noqa: ARG002 - fails before reading it
            raise RuntimeError("boom")

    app = app_of(BrokenViewSet)

    @app.exception_handler(Exception)
    async def crashed(_request, _error):
        return JSONResponse({"detail": "crashed"}, status_code=500)

    stream = FakeStream({":method": "GET", ":path": "/broken"})
    await process_command(ABSENT, stream, app=app)
    assert stream.status == 500
    assert stream.payload == {"detail": "crashed"}


def broken_app() -> FastAPI:
    router = APIRouter()

    @route_viewset(router, base_path="/broken", pk_field_name="id")
    class BrokenViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container={}, pk_field="id")

        async def perform_list(self, context: Context) -> list[Track]:  # noqa: ARG002 - fails before reading it
            raise RuntimeError("secret detail of the failure")

    return app_of(BrokenViewSet)


UNHANDLED = {
    "detail": "Internal Server Error",
    "detail_code": "fastapi-viewsets.unhandled_error",
    "detail_params": {},
    "field_errors": {},
}


@pytest.mark.asyncio
async def test_an_unhandled_error_is_answered_500_unhandled_error_and_logged(caplog):
    stream = FakeStream({":method": "GET", ":path": "/broken"})
    with caplog.at_level(logging.ERROR):
        await process_command(ABSENT, stream, app=broken_app())
    assert (stream.status, stream.payload) == (500, UNHANDLED)
    assert "secret detail of the failure" in caplog.text


@pytest.mark.asyncio
async def test_an_app_that_raises_before_it_responds_is_answered_500_unhandled_error():
    async def raw_app(_scope, _receive, _send):
        raise RuntimeError("secret detail of the failure")

    make_viewset()
    stream = FakeStream({":method": "GET", ":path": "/tracks"})
    await process_command(ABSENT, stream, app=raw_app)
    assert (stream.status, stream.payload) == (500, UNHANDLED)


@pytest.mark.asyncio
async def test_dependencies_on_the_router_guard_both_transports():
    from fastapi import Depends, HTTPException, Request

    async def gate(request: Request) -> None:
        if request.headers.get("x-pass") != "1":
            raise HTTPException(status_code=403)

    database = {1: Track(id=1, title="Kind of Blue")}
    router = APIRouter(dependencies=[Depends(gate)])

    @route_viewset(router, base_path="/gated", pk_field_name="id")
    class GatedViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=database, pk_field="id")

    app = app_of(GatedViewSet)
    assert TestClient(app).get("/gated").status_code == 403

    refused = FakeStream({":method": "GET", ":path": "/gated"})
    allowed = FakeStream({":method": "GET", ":path": "/gated", "x-pass": "1"})
    await process_command(ABSENT, refused, app=app)
    await process_command(ABSENT, allowed, app=app)
    assert (refused.status, allowed.status) == (403, 200)


@pytest.mark.asyncio
async def test_the_endpoint_sees_the_apps_state_and_the_lifespan_state():
    captured: dict[str, Any] = {}
    router = APIRouter()

    @route_viewset(router, base_path="/stateful", pk_field_name="id")
    class StatefulViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        __router = APIRouter()

        @__router.get("probe", tags=["Stateful"])
        async def probe(self, request: Request) -> int:
            captured["app"] = request.app.state.name
            captured["lifespan"] = request.state.pool
            request.state.pool = "replaced"
            return 1

        def __init__(self):
            super().__init__(container={}, pk_field="id")

    app = app_of(StatefulViewSet)
    app.state.name = "the application"
    lifespan_state = {"pool": "the pool"}

    stream = FakeStream({":method": "GET", ":path": "/stateful/probe"})
    await process_command(ABSENT, stream, connection=FakeConnection(app, state=lifespan_state))
    assert stream.status == 200
    assert captured == {"app": "the application", "lifespan": "the pool"}
    assert lifespan_state == {"pool": "the pool"}


@pytest.mark.asyncio
async def test_dependency_overrides_apply_to_a_command():
    from fastapi import Depends

    def real_limit() -> int:
        return 1

    router = APIRouter()

    @route_viewset(router, base_path="/overridden", pk_field_name="id")
    class OverriddenViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        __router = APIRouter()

        @__router.get("limit", tags=["Overridden"])
        async def limit(self, value: int = Depends(real_limit)) -> int:  # noqa: B008
            return value

        def __init__(self):
            super().__init__(container={}, pk_field="id")

    app = app_of(OverriddenViewSet)
    app.dependency_overrides[real_limit] = lambda: 7

    stream = FakeStream({":method": "GET", ":path": "/overridden/limit"})
    await process_command(ABSENT, stream, app=app)
    assert stream.payload == 7


@pytest.mark.asyncio
async def test_a_compression_middleware_leaves_the_reply_uncompressed():
    from starlette.middleware.gzip import GZipMiddleware

    cls = make_viewset()
    app = app_of(cls)
    app.add_middleware(GZipMiddleware, minimum_size=1)
    connection = FakeConnection(
        app,
        headers={
            "Accept-Encoding": "gzip, deflate",
            "Connection": "Upgrade",
            "Upgrade": "websocket",
            "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
            "Cookie": "session=abc",
        },
    )
    captured: dict[str, str] = {}

    async def capture(request, _viewset) -> dict[str, Any]:
        captured.update(request.headers)
        return {}

    settings.viewsets_context_processors = [capture]
    try:
        stream = FakeStream({":method": "GET", ":path": "/tracks"})
        await process_command(ABSENT, stream, connection=connection)
    finally:
        settings.viewsets_context_processors = []
    assert stream.payload[0] == {"id": 1, "title": "Kind of Blue"}
    assert "content-encoding" not in stream.reply_headers
    assert captured["cookie"] == "session=abc"
    assert not {"accept-encoding", "connection", "upgrade", "sec-websocket-key"} & set(captured)


@pytest.mark.asyncio
async def test_the_path_carries_the_include_router_prefix():
    cls = make_viewset()
    app = app_of(cls, prefix="/api")

    prefixed = FakeStream({":method": "GET", ":path": "/api/tracks/1"})
    bare = FakeStream({":method": "GET", ":path": "/tracks/1"})
    await process_command(ABSENT, prefixed, app=app)
    await process_command(ABSENT, bare, app=app)
    assert TestClient(app).get("/api/tracks/1").json() == prefixed.payload == {"id": 1, "title": "Kind of Blue"}
    assert bare.status == 404


@pytest.mark.asyncio
async def test_each_transport_answers_schema_with_its_own_endpoints():
    database = {1: Track(id=1, title="Kind of Blue")}
    router = APIRouter()

    @route_viewset(router, base_path="/both", pk_field_name="id")
    class BothViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        __router = APIRouter()

        @transports(muxws=False)
        @__router.get("rest-only", tags=["Both"])
        async def rest_only(self) -> int:
            return 1

        @transports(rest=False)
        @__router.get("muxws-only", tags=["Both"])
        async def muxws_only(self) -> int:
            return 2

        def __init__(self):
            super().__init__(container=database, pk_field="id")

    app = app_of(BothViewSet)
    stream = FakeStream({":method": "GET", ":path": "/both/schema"})
    await process_command(ABSENT, stream, app=app)
    rest_paths = TestClient(app).get("/both/schema").json()["paths"]
    assert "/both/rest-only" in rest_paths
    assert "/both/muxws-only" not in rest_paths
    assert "/both/rest-only" not in stream.payload["paths"]
    assert "/both/muxws-only" in stream.payload["paths"]
    assert "/both" in rest_paths
    assert "/both" in stream.payload["paths"]


@pytest.mark.asyncio
async def test_a_request_whose_stream_ended_runs_to_completion_and_its_answer_is_discarded():
    import asyncio

    started, release = asyncio.Event(), asyncio.Event()
    finished: list[str] = []
    router = APIRouter()

    @route_viewset(router, base_path="/slow")
    class SlowViewSet:
        __router = APIRouter()

        @__router.post("")
        async def create(self) -> dict:
            started.set()
            await release.wait()
            finished.append("create")
            return {"ok": True}

    stream = FakeStream({":method": "POST", ":path": "/slow"})
    handler = asyncio.ensure_future(process_command(ABSENT, stream, app=app_of(SlowViewSet)))
    await started.wait()
    handler.cancel()  # what muxws does to the handler when the stream is reset or the socket lost
    with pytest.raises(asyncio.CancelledError):
        await handler

    release.set()
    for _ in range(100):
        if finished:
            break
        await asyncio.sleep(0.01)
    assert finished == ["create"]
    assert stream.replies == 0


@pytest.mark.asyncio
async def test_the_request_scope_carries_the_peer():
    captured: list[Any] = []
    router = APIRouter()

    @route_viewset(router, base_path="/peered", pk_field_name="id")
    class PeeredViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        __router = APIRouter()

        @__router.get("probe", tags=["Peered"])
        async def probe(self, request: Request) -> int:
            captured.append(request.scope["fastapi_viewsets.peer"])
            return 1

        def __init__(self):
            super().__init__(container={}, pk_field="id")

    app = app_of(PeeredViewSet)
    peer = object()
    await process_command(ABSENT, FakeStream({":method": "GET", ":path": "/peered/probe"}), app=app, peer=peer)
    await process_command(ABSENT, FakeStream({":method": "GET", ":path": "/peered/probe"}), app=app)
    assert captured[0] is peer
    assert captured[1] is None
