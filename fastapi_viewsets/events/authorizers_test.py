import pytest

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from ..action_configuration import action_configuration
from ..collection_viewset import CollectionViewSet
from ..conf import settings
from ..context import Context
from ..decorators import route_viewset
from ..exceptions import EventTopicRefusedError
from ..middleware import Middleware
from ..middleware.auth import Session
from ..middleware.auth.authorization import Authorization
from ..middleware.rate_limiter import RateLimiter
from ..mixins import RetrieveMixin, ViewSetMixin
from ..mux_ws.registry import reset_registry
from .authorizers import (
    allow_all,
    allow_authenticated,
    authorize_topic,
    DeclaredSources,
    find_viewset,
    match_viewset,
    ViewsetTopics,
)
from .sources import source


class Track(BaseModel):
    id: int = Field(json_schema_extra={"autoinc_int": True})
    title: str


class FakeRequest:
    """The part of a Request the authorizers read: `app`, and `client` for `RateLimiter`."""

    def __init__(self, app: FastAPI | None = None):
        self.app = app
        self.client = None


@pytest.fixture(autouse=True)
def _reset_application_settings():
    saved = settings.viewsets_command_middleware, settings.viewsets_context_processors
    reset_registry()
    yield
    settings.viewsets_command_middleware, settings.viewsets_context_processors = saved
    reset_registry()


def music_router(base_path: str = "/music") -> tuple[APIRouter, type]:
    router = APIRouter()

    @route_viewset(router, base_path=base_path, pk_field_name="id")
    class MusicViewSet(CollectionViewSet[int, Track], ViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container={1: Track(id=1, title="Blue Train")}, pk_field="id")

    return router, MusicViewSet


def app_with(router: APIRouter, prefix: str = "") -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix=prefix)
    return app


# authorize_topic


@pytest.mark.asyncio
async def test_no_authorizer_refuses_every_topic():
    with pytest.raises(EventTopicRefusedError) as raised:
        await authorize_topic(FakeRequest(), Context({}), "imports")
    assert raised.value.status_code == 403
    assert raised.value.code == "event_topic_refused"
    assert raised.value.params == {"topic": "imports"}


@pytest.mark.asyncio
async def test_the_first_decision_wins():
    calls = []

    def answering(answer):
        async def authorizer(_request, _context, _topic):
            calls.append(answer)
            return answer

        return authorizer

    settings.viewsets_event_authorizers = [answering(None), answering(True), answering(False)]
    await authorize_topic(FakeRequest(), Context({}), "imports")
    assert calls == [None, True]

    calls.clear()
    settings.viewsets_event_authorizers = [answering(None), answering(False), answering(True)]
    with pytest.raises(EventTopicRefusedError):
        await authorize_topic(FakeRequest(), Context({}), "imports")
    assert calls == [None, False]


@pytest.mark.asyncio
async def test_only_opinions_none_refuse():
    async def no_opinion(_request, _context, _topic):
        return None

    settings.viewsets_event_authorizers = [no_opinion, no_opinion]
    with pytest.raises(EventTopicRefusedError):
        await authorize_topic(FakeRequest(), Context({}), "imports")


@pytest.mark.asyncio
async def test_an_http_exception_of_an_authorizer_propagates():
    async def gone(_request, _context, _topic):
        raise HTTPException(410, "gone")

    settings.viewsets_event_authorizers = [gone, allow_all]
    with pytest.raises(HTTPException) as raised:
        await authorize_topic(FakeRequest(), Context({}), "imports")
    assert raised.value.status_code == 410


@pytest.mark.asyncio
async def test_authorizers_receive_request_context_and_topic():
    received = []

    async def recording(request, context, topic):
        received.append((request, context, topic))
        return True

    request, context = FakeRequest(), Context({})
    settings.viewsets_event_authorizers = [recording]
    await authorize_topic(request, context, "imports:7")
    assert received == [(request, context, "imports:7")]


# allow_all, allow_authenticated


@pytest.mark.asyncio
async def test_allow_all_admits_every_topic():
    assert await allow_all(FakeRequest(), Context({}), "/anything") is True


@pytest.mark.asyncio
async def test_allow_authenticated_admits_a_caller_with_a_user():
    assert await allow_authenticated(FakeRequest(), Context({"user": "jure"}), "imports") is True
    assert await allow_authenticated(FakeRequest(), Context({"user": None}), "imports") is None
    assert await allow_authenticated(FakeRequest(), Context({}), "imports") is None


# DeclaredSources


@pytest.mark.asyncio
async def test_declared_sources_asks_the_source_authorizer():
    received = []

    async def club_member(_request, _context, topic):
        received.append(topic)
        return topic == "clubs:7"

    source("clubs", authorizer=club_member)
    authorizer = DeclaredSources()
    assert await authorizer(FakeRequest(), Context({}), "clubs:7") is True
    assert await authorizer(FakeRequest(), Context({}), "clubs:8") is False
    assert received == ["clubs:7", "clubs:8"]


@pytest.mark.asyncio
async def test_declared_sources_has_no_opinion_without_a_source_authorizer():
    source("imports")
    authorizer = DeclaredSources()
    assert await authorizer(FakeRequest(), Context({}), "imports:7") is None
    assert await authorizer(FakeRequest(), Context({}), "undeclared") is None
    assert await authorizer(FakeRequest(), Context({}), "/api/music") is None


# ViewsetTopics


@pytest.mark.asyncio
async def test_viewset_topics_admits_the_routed_base_path():
    router, cls = music_router()
    app = app_with(router, prefix="/api")
    assert find_viewset(app, "/api/music") == (cls, "list_items")
    assert await ViewsetTopics()(FakeRequest(app), Context({}), "/api/music") is True


@pytest.mark.asyncio
async def test_viewset_topics_has_no_opinion_on_other_topics():
    router, _cls = music_router()
    app = app_with(router, prefix="/api")
    authorizer = ViewsetTopics()
    for topic in ("imports", "/music", "/api/music/1", "/api", "/api/music/schema"):
        assert await authorizer(FakeRequest(app), Context({}), topic) is None, topic


@pytest.mark.asyncio
async def test_a_viewset_without_list_items_is_found_by_retrieve():
    router = APIRouter()

    @route_viewset(router, base_path="/tracks", pk_field_name="id")
    class TrackViewSet(RetrieveMixin[int, Track]):
        async def perform_retrieve(self, _context: Context, pk: int) -> Track:
            return Track(id=pk, title="Giant Steps")

    app = app_with(router, prefix="/v1")
    assert find_viewset(app, "/v1/tracks") == (TrackViewSet, "retrieve")
    assert await ViewsetTopics()(FakeRequest(app), Context({}), "/v1/tracks") is True


@pytest.mark.asyncio
async def test_one_viewset_mounted_twice_has_two_topics():
    router, cls = music_router()
    app = FastAPI()
    app.include_router(router, prefix="/a")
    app.include_router(router, prefix="/b")
    assert find_viewset(app, "/a/music") == (cls, "list_items")
    assert find_viewset(app, "/b/music") == (cls, "list_items")
    assert find_viewset(app, "/music") is None


@pytest.mark.asyncio
async def test_viewset_topics_runs_session_depends():
    settings.viewsets_command_middleware = [Session()]
    router, _cls = music_router()
    app = app_with(router)
    authorizer = ViewsetTopics()

    assert await authorizer(FakeRequest(app), Context({"user": "jure"}), "/music") is True
    with pytest.raises(HTTPException) as raised:
        await authorizer(FakeRequest(app), Context({"user": None}), "/music")
    assert raised.value.status_code == 401
    assert raised.value.code == "session_expired"


@pytest.mark.asyncio
async def test_viewset_topics_resolves_the_action_configuration_of_the_viewset():
    settings.viewsets_command_middleware = [Session(), Authorization()]
    seen = []

    async def admin_only(_request, cls, context):
        seen.append((cls, context._action_name))
        return await context.user == "admin"

    router, cls = music_router()
    action_configuration({Authorization: admin_only, Session: False})(cls)
    app = app_with(router)
    authorizer = ViewsetTopics()

    assert await authorizer(FakeRequest(app), Context({"user": "admin"}), "/music") is True
    with pytest.raises(HTTPException) as raised:
        await authorizer(FakeRequest(app), Context({"user": None}), "/music")
    assert raised.value.status_code == 403
    assert raised.value.code == "not_authorized"
    assert seen == [(cls, "list_items"), (cls, "list_items")]


@pytest.mark.asyncio
async def test_viewset_topics_rebinds_and_leaves_the_calling_context_unchanged():
    received = []

    class Recording(Middleware):
        async def depends(self, request, cls, context):
            received.append((request, cls, context))

        async def __call__(self, _request, _viewset, _context, call_next):
            return await call_next()

    recording = Recording()
    settings.viewsets_command_middleware = [recording]
    router, cls = music_router()
    action_configuration({Recording: "music"})(cls)
    app = app_with(router)
    request = FakeRequest(app)
    calling = Context({"user": "jure"}, action_configuration={Recording: "events"}, action_name="poll")

    assert await ViewsetTopics()(request, calling, "/music") is True
    [(seen_request, seen_cls, bound)] = received
    assert seen_request is request
    assert seen_cls is cls
    assert bound is not calling
    assert bound.raw() is calling.raw()
    assert recording.config_from(bound) == "music"
    assert recording.config_from(calling) == "events"


@pytest.mark.asyncio
async def test_viewset_topics_runs_a_middleware_the_configuration_adds():
    limiter = RateLimiter(default_limit=1)
    router, cls = music_router()
    action_configuration({limiter: 1})(cls)
    app = app_with(router)
    authorizer = ViewsetTopics()

    assert await authorizer(FakeRequest(app), Context({}), "/music") is True
    with pytest.raises(HTTPException) as raised:
        await authorizer(FakeRequest(app), Context({}), "/music")
    assert raised.value.status_code == 429


@pytest.mark.asyncio
async def test_a_topic_matches_a_routed_base_path_with_path_parameters():
    router, cls = music_router()
    app = app_with(router, prefix="/clubs/{club_id:int}")
    found = match_viewset(app, "/clubs/7/music")
    assert (found.viewset_class, found.action, found.path_params) == (cls, "list_items", {"club_id": 7})
    assert find_viewset(app, "/clubs/7/music") == (cls, "list_items")
    for topic in ("/clubs/seven/music", "/clubs/7/music/1", "/clubs/music"):
        assert match_viewset(app, topic) is None, topic


@pytest.mark.asyncio
async def test_viewset_topics_hands_depends_the_path_parameters_of_the_topic():
    received = []

    class Recording(Middleware):
        async def depends(self, request, _cls, _context):
            received.append(request.path_params)

        async def __call__(self, _request, _viewset, _context, call_next):
            return await call_next()

    settings.viewsets_command_middleware = [Recording()]
    router, _cls = music_router()
    app = app_with(router, prefix="/clubs/{club_id}")
    request = Request({"type": "http", "method": "POST", "path": "/events/poll", "headers": [], "app": app})
    assert await ViewsetTopics()(request, Context({}), "/clubs/7/music") is True
    assert received == [{"club_id": "7"}]
    assert request.path_params == {}


def test_find_viewset_reads_the_routes_of_a_fastapi_without_route_contexts(monkeypatch):
    import fastapi.routing

    monkeypatch.delattr(fastapi.routing, "iter_route_contexts")
    app = FastAPI()

    @route_viewset(app.router, base_path="/music", pk_field_name="id")
    class MusicViewSet(CollectionViewSet[int, Track], ViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container={}, pk_field="id")

    assert find_viewset(app, "/music") == (MusicViewSet, "list_items")
    assert find_viewset(app, "/music/1") is None
