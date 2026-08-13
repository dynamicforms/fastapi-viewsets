from typing import Any

import pytest

from fastapi import APIRouter
from muxws.frames import ABSENT
from pydantic import BaseModel, Field

from ..collection_viewset import CollectionViewSet
from ..conf import settings
from ..context import Context
from ..decorators import route_viewset
from ..mixins import BulkViewSetMixin
from .registry import get_dispatch_app, is_registered, registered_viewsets, reset_registry, schema_for
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
    make_viewset()
    stream = FakeStream({"action": "quote", "symbol": "ACME"})
    assert await process_command({"any": "payload"}, stream) is False
    assert stream.replies == 0


@pytest.mark.asyncio
async def test_list_returns_the_collection():
    make_viewset()
    stream = FakeStream({":method": "GET", ":path": "/tracks"})
    assert await process_command(ABSENT, stream) is True
    assert stream.status == 200
    assert stream.payload == [
        {"id": 1, "title": "Kind of Blue"},
        {"id": 2, "title": "Blue Train"},
    ]


@pytest.mark.asyncio
async def test_retrieve_binds_the_path_parameter():
    make_viewset()
    stream = FakeStream({":method": "GET", ":path": "/tracks/2"})
    await process_command(ABSENT, stream)
    assert stream.status == 200
    assert stream.payload == {"id": 2, "title": "Blue Train"}


@pytest.mark.asyncio
async def test_create_binds_the_payload_as_the_request_body():
    make_viewset()
    stream = FakeStream({":method": "POST", ":path": "/tracks"})
    await process_command({"id": 3, "title": "Giant Steps"}, stream)
    assert stream.status == 200
    assert stream.payload["title"] == "Giant Steps"


@pytest.mark.asyncio
async def test_a_missing_record_answers_with_a_status_not_a_reset():
    """
    404 is an outcome, not a transport failure. Resetting the stream would tell the caller the
    connection misbehaved, when in fact the server answered perfectly well.
    """
    make_viewset()
    stream = FakeStream({":method": "GET", ":path": "/tracks/999"})
    assert await process_command(ABSENT, stream) is True
    assert stream.status == 404


@pytest.mark.asyncio
async def test_validation_errors_come_back_as_422():
    make_viewset()
    stream = FakeStream({":method": "POST", ":path": "/tracks"})
    await process_command({"title": 12345, "id": "not-an-int"}, stream)
    assert stream.status == 422


@pytest.mark.asyncio
async def test_an_unregistered_path_answers_404():
    make_viewset()
    stream = FakeStream({":method": "GET", ":path": "/nonexistent"})
    assert await process_command(ABSENT, stream) is True
    assert stream.status == 404


@pytest.mark.asyncio
async def test_query_parameters_reach_the_endpoint():
    make_viewset()
    stream = FakeStream({":method": "GET", ":path": "/tracks", ":query": {"sort": "id:desc"}})
    await process_command(ABSENT, stream)
    assert stream.status == 200
    assert [record["id"] for record in stream.payload] == [2, 1]


@pytest.mark.asyncio
async def test_schema_reports_the_muxws_endpoint_set():
    make_viewset()
    stream = FakeStream({":method": "GET", ":path": "/tracks/schema"})
    await process_command(ABSENT, stream)
    assert stream.status == 200
    assert "/tracks" in stream.payload["paths"]


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

        stream = FakeStream({
            ":method": "GET",
            ":path": "/ctx",
            "authorization": "Bearer per-call",
        })
        await process_command(
            ABSENT, stream, base_headers={"authorization": "Bearer handshake", "x-trace": "abc"}
        )
        assert stream.status == 200
        assert captured["authorization"] == "Bearer per-call"
        assert captured["x-trace"] == "abc"
    finally:
        settings.viewsets_context_processors = []


@pytest.mark.asyncio
async def test_register_muxws_false_keeps_the_viewset_off_the_transport():
    cls = make_viewset(register_muxws=False)
    assert not is_registered(cls)
    stream = FakeStream({":method": "GET", ":path": "/tracks"})
    await process_command(ABSENT, stream)
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

    rest_paths = {route.path for route in rest_router.routes}
    assert "/split/live" not in rest_paths

    stream = FakeStream({":method": "GET", ":path": "/split/live"})
    await process_command(ABSENT, stream)
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

    assert "/restonly/secret" in {route.path for route in rest_router.routes}
    # Asserted against the dispatch app rather than by calling it: /restonly/secret would be
    # matched by the /restonly/{pk} route and fail pk validation, so a request-level assertion
    # would pass whether or not the endpoint had been excluded.
    assert "/restonly/secret" not in {route.path for route in get_dispatch_app().router.routes}


def test_transports_refuses_an_unreachable_endpoint():
    with pytest.raises(ValueError, match="unreachable"):
        transports(rest=False, muxws=False)


@pytest.mark.asyncio
async def test_re_registering_the_same_class_replaces_rather_than_duplicates():
    """Re-importing a module re-runs the decorator; that must not shadow the first registration."""
    database = {1: Track(id=1, title="Kind of Blue")}
    router = APIRouter()

    class TwiceViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=database, pk_field="id")

    for _ in range(2):
        route_viewset(router, base_path="/twice", pk_field_name="id")(TwiceViewSet)

    paths = [route.path for route in get_dispatch_app().router.routes]
    assert paths.count("/twice/schema") == 1


def test_registry_reports_registered_viewsets():
    cls = make_viewset()
    assert registered_viewsets()[cls] == "/tracks"
    assert schema_for("/tracks") is not None
    assert schema_for("/nope") is None
