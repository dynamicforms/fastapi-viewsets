import logging

from typing import Any

import pytest

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Path, Request
from httpx2 import ASGITransport, AsyncClient
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..action_configuration import action_configuration
from ..collection_viewset import CollectionViewSet
from ..conf import settings
from ..decorators import route_viewset
from ..exceptions import df_viewset_exception_handler, DfViewSetError
from ..middleware.auth import Session
from ..middleware.auth.authorization import Authorization
from ..middleware.idempotency import Idempotency
from ..middleware.rate_limiter import RateLimiter
from ..mixins import ViewSetMixin
from ..mux_ws.registry import reset_registry
from ..mux_ws.server import process_command
from .authorizers import allow_all, allow_authenticated, DeclaredSources, ViewsetTopics
from .changes import ChangeEvents
from .conftest import FakePeer
from .delivery import subscriptions, topics_of, wait_for_pushes
from .event import Event
from .sources import publish, source
from .viewset import register_events


class Track(BaseModel):
    id: int = Field(json_schema_extra={"autoinc_int": True})
    title: str


class TrackAdded(Event):
    event_name = "track_added"

    title: str


class TrackAddedV2(Event):
    event_name = "track_added"
    event_version = 2

    title: str
    seconds: int


class FakeStream:
    """The part of a muxws Stream `process_command` uses: `headers` and one `reply`."""

    def __init__(self, headers: dict[str, Any]):
        self.headers = headers
        self.payload: Any = None
        self.reply_headers: dict[str, Any] = {}
        self.trailers: Any = None

    async def reply(self, payload: Any, *, headers: dict[str, Any] | None = None, trailers: Any = None) -> None:
        self.payload = payload
        self.reply_headers = headers or {}
        self.trailers = trailers

    @property
    def status(self) -> int | None:
        return self.reply_headers.get(":status")


async def user_processor(request: Request, _viewset: Any) -> dict[str, Any]:
    return {"user": request.headers.get("x-user")}


@pytest.fixture(autouse=True)
def _reset_application_settings():
    saved = settings.viewsets_command_middleware, settings.viewsets_context_processors
    settings.viewsets_context_processors = [user_processor]
    reset_registry()
    yield
    settings.viewsets_command_middleware, settings.viewsets_context_processors = saved
    reset_registry()


class Application:
    """A music viewset mounted under `/api` and the events viewset under `/events`."""

    def __init__(self):
        music_router = APIRouter()

        @route_viewset(music_router, base_path="/music", pk_field_name="id")
        class MusicViewSet(CollectionViewSet[int, Track], ViewSetMixin[int, Track]):
            def __init__(self):
                super().__init__(container={1: Track(id=1, title="Blue Train")}, pk_field="id")

        events_router = APIRouter()
        self.music = MusicViewSet
        self.events = register_events(events_router)
        self.app = FastAPI()
        self.app.add_exception_handler(DfViewSetError, df_viewset_exception_handler)
        self.app.include_router(music_router, prefix="/api")
        self.app.include_router(events_router)

    async def command(self, path: str, payload: Any, peer: Any = None, **headers: str) -> FakeStream:
        stream = FakeStream({":method": "POST", ":path": path, **headers})
        assert await process_command(payload, stream, app=self.app, peer=peer) is True
        return stream

    async def rest(self, path: str, payload: Any, method: str = "POST", **headers: str):
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as client:
            return await client.request(method, path, json=payload, headers=headers)


@pytest.fixture
def application() -> Application:
    return Application()


# subscribe and unsubscribe


@pytest.mark.asyncio
async def test_subscribe_answers_the_current_rev(application):
    settings.viewsets_event_authorizers = [allow_all]
    peer = FakePeer()

    stream = await application.command("/events/subscribe", {"topic": "/api/music"}, peer)
    assert stream.status == 200
    assert stream.payload == {"rev": 0}
    assert topics_of(peer) == ["/api/music"]

    rev, _prev = await publish("/api/music", TrackAdded(title="Giant Steps"))
    stream = await application.command("/events/subscribe", {"topic": "/api/music"}, peer)
    assert stream.payload == {"rev": rev}
    assert len(subscriptions("/api/music")) == 1


@pytest.mark.asyncio
async def test_subscribe_and_unsubscribe_are_not_on_rest(application):
    settings.viewsets_event_authorizers = [allow_all]
    for path in ("/events/subscribe", "/events/unsubscribe"):
        response = await application.rest(path, {"topic": "/api/music"})
        assert response.status_code in (404, 405), path


@pytest.mark.asyncio
async def test_subscribe_without_peer_raises_naming_the_argument(application, caplog):
    settings.viewsets_event_authorizers = [allow_all]
    with caplog.at_level(logging.ERROR):
        stream = await application.command("/events/subscribe", {"topic": "/api/music"})
    assert stream.status == 500
    assert "peer=" in caplog.text
    assert subscriptions("/api/music") == []


@pytest.mark.asyncio
async def test_subscribe_refuses_a_topic_of_neither_form(application):
    settings.viewsets_event_authorizers = [allow_all]
    stream = await application.command("/events/subscribe", {"topic": "Not A Topic"}, FakePeer())
    assert stream.status == 422
    assert stream.payload["detail_code"] == "event_topic_invalid"


@pytest.mark.asyncio
async def test_subscribe_refuses_a_topic_no_authorizer_admits(application):
    peer = FakePeer()
    stream = await application.command("/events/subscribe", {"topic": "/api/music"}, peer)
    assert stream.status == 403
    assert stream.payload["detail_code"] == "event_topic_refused"
    assert stream.payload["detail_params"] == {"topic": "/api/music"}
    assert topics_of(peer) == []


@pytest.mark.asyncio
async def test_unsubscribe_answers_204_also_for_a_topic_not_held(application):
    settings.viewsets_event_authorizers = [allow_all]
    peer = FakePeer()
    await application.command("/events/subscribe", {"topic": "/api/music"}, peer)

    stream = await application.command("/events/unsubscribe", {"topic": "/api/music"}, peer)
    assert stream.status == 204
    assert stream.payload is None
    assert topics_of(peer) == []

    stream = await application.command("/events/unsubscribe", {"topic": "imports:7"}, peer)
    assert stream.status == 204


@pytest.mark.asyncio
async def test_a_socket_holds_at_most_the_subscription_limit(application):
    settings.viewsets_event_authorizers = [allow_all]
    settings.viewsets_event_max_subscriptions = 2
    peer = FakePeer()
    for topic in ("a", "b"):
        assert (await application.command("/events/subscribe", {"topic": topic}, peer)).status == 200

    stream = await application.command("/events/subscribe", {"topic": "c"}, peer)
    assert stream.status == 409
    assert stream.payload["detail_code"] == "event_subscriptions_exceeded"
    assert stream.payload["detail_params"] == {"limit": 2}
    assert sorted(topics_of(peer)) == ["a", "b"]

    assert (await application.command("/events/subscribe", {"topic": "a"}, peer)).status == 200
    assert (await application.command("/events/subscribe", {"topic": "c"}, FakePeer())).status == 200


@pytest.mark.asyncio
async def test_a_closed_socket_drops_its_subscriptions(application):
    settings.viewsets_event_authorizers = [allow_all]
    peer, other = FakePeer(), FakePeer()
    await application.command("/events/subscribe", {"topic": "/api/music"}, peer)
    await application.command("/events/subscribe", {"topic": "imports"}, peer)
    await application.command("/events/subscribe", {"topic": "/api/music"}, other)
    assert len(peer.close_handlers) == 1

    peer.close()
    await wait_for_pushes()
    assert topics_of(peer) == []
    assert [subscription.peer for subscription in subscriptions("/api/music")] == [other]
    assert subscriptions("imports") == []


@pytest.mark.asyncio
async def test_an_event_reaches_the_subscribed_socket(application):
    settings.viewsets_event_authorizers = [allow_all]
    peer, bystander = FakePeer(), FakePeer()
    await application.command("/events/subscribe", {"topic": "/api/music"}, peer)
    await application.command("/events/subscribe", {"topic": "imports"}, bystander)

    rev, prev = await publish("/api/music", TrackAdded(title="Giant Steps"))
    await wait_for_pushes()
    assert peer.pushes == [
        (
            {"title": "Giant Steps"},
            {":event": "track_added", ":version": 1, ":topic": "/api/music", ":rev": rev, ":prev": prev},
        )
    ]
    assert bystander.pushes == []


@pytest.mark.asyncio
async def test_the_subscription_carries_the_context_of_its_subscribe(application):
    settings.viewsets_event_authorizers = [allow_all]
    peer = FakePeer()
    await application.command("/events/subscribe", {"topic": "/api/music"}, peer, **{"x-user": "jure"})
    [subscription] = subscriptions("/api/music")
    assert subscription.peer is peer
    assert await subscription.context.user == "jure"


# subscribe through ViewsetTopics


@pytest.mark.asyncio
async def test_subscribe_through_viewset_topics_follows_session(application):
    settings.viewsets_command_middleware = [Session()]
    settings.viewsets_event_authorizers = [ViewsetTopics()]
    action_configuration({Session: False})(application.events)
    peer = FakePeer()

    stream = await application.command("/events/subscribe", {"topic": "/api/music"}, peer)
    assert stream.status == 401
    assert stream.payload["detail_code"] == "session_expired"
    assert topics_of(peer) == []

    stream = await application.command("/events/subscribe", {"topic": "/api/music"}, peer, **{"x-user": "jure"})
    assert stream.status == 200
    assert topics_of(peer) == ["/api/music"]


@pytest.mark.asyncio
async def test_subscribe_through_viewset_topics_follows_authorization(application):
    async def admin_only(_request, _cls, context):
        return await context.user == "admin"

    settings.viewsets_command_middleware = [Authorization()]
    settings.viewsets_event_authorizers = [ViewsetTopics()]
    action_configuration({Authorization: admin_only})(application.music)
    peer = FakePeer()

    stream = await application.command("/events/subscribe", {"topic": "/api/music"}, peer, **{"x-user": "jure"})
    assert stream.status == 403
    assert stream.payload["detail_code"] == "not_authorized"

    stream = await application.command("/events/subscribe", {"topic": "/api/music"}, peer, **{"x-user": "admin"})
    assert stream.status == 200


@pytest.mark.asyncio
async def test_viewset_topics_uses_the_prefixed_path(application):
    settings.viewsets_event_authorizers = [ViewsetTopics()]
    peer = FakePeer()
    assert (await application.command("/events/subscribe", {"topic": "/api/music"}, peer)).status == 200

    stream = await application.command("/events/subscribe", {"topic": "/music"}, peer)
    assert stream.status == 403
    assert stream.payload["detail_code"] == "event_topic_refused"


@pytest.mark.asyncio
async def test_the_events_viewset_passes_through_the_command_middleware(application):
    settings.viewsets_command_middleware = [Session()]
    settings.viewsets_event_authorizers = [allow_all]

    stream = await application.command("/events/subscribe", {"topic": "imports"}, FakePeer())
    assert stream.status == 401
    response = await application.rest("/events/poll", {"cursors": {"imports": None}})
    assert response.status_code == 401


# poll


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["rest", "muxws"])
async def test_poll_answers_every_cursor(application, transport):
    settings.viewsets_event_authorizers = [ViewsetTopics(), DeclaredSources()]
    settings.viewsets_event_poll_after_ms = 250
    source("imports", authorizer=allow_all)
    source("orgs")
    first, _ = await publish("/api/music", TrackAdded(title="Blue Train"))
    second, _ = await publish("/api/music", TrackAdded(title="Giant Steps"))
    third, _ = await publish("/api/music", TrackAddedV2(title="Naima", seconds=261))
    imported, _ = await publish("imports:7", TrackAdded(title="Moment's Notice"))

    body = {
        "cursors": {
            "/api/music": first,
            "imports:7": None,
            "imports:8": None,
            "orgs:9": 3,
            "imports": imported,
            "Not A Topic": None,
        }
    }
    if transport == "rest":
        response = await application.rest("/events/poll", body)
        status, answer = response.status_code, response.json()
    else:
        stream = await application.command("/events/poll", body)
        status, answer = stream.status, stream.payload

    assert status == 200
    assert answer == {
        "topics": {
            "/api/music": {
                "rev": third,
                "events": [
                    {
                        "rev": second,
                        "prev": first,
                        "event": "track_added",
                        "version": 1,
                        "payload": {"title": "Giant Steps"},
                    },
                    {
                        "rev": third,
                        "prev": second,
                        "event": "track_added",
                        "version": 2,
                        "payload": {"title": "Naima", "seconds": 261},
                    },
                ],
            },
            "imports:7": {"rev": imported, "events": []},
            "imports:8": {"rev": 0, "events": []},
            "orgs:9": {"rev": None, "events": None, "error": "event_topic_refused", "status": 403},
            "imports": {"rev": 0, "events": None},
            "Not A Topic": {"rev": None, "events": None, "error": "event_topic_invalid", "status": 422},
        },
        "poll_after_ms": 250,
    }
    assert list(answer["topics"]) == list(body["cursors"])


def refuse_secrets(seen: list | None = None):
    """A delivery filter that refuses every event whose payload's title is "secret", recording each call."""

    async def refuse(subscription, topic, rev, _name, payload, _version):
        if seen is not None:
            seen.append((subscription.peer, await subscription.context.user, subscription.topic, topic, rev))
        return payload["title"] != "secret"

    return refuse


def polled(rev: int, prev: int, title: str) -> dict[str, Any]:
    return {"rev": rev, "prev": prev, "event": "track_added", "version": 1, "payload": {"title": title}}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["rest", "muxws"])
async def test_poll_leaves_out_the_events_a_delivery_filter_refuses(application, transport):
    settings.viewsets_event_authorizers = [allow_all]
    seen: list = []
    settings.viewsets_event_delivery_filters = [refuse_secrets(seen)]
    first, _ = await publish("imports", TrackAdded(title="Naima"))
    second, _ = await publish("imports", TrackAdded(title="secret"))
    third, _ = await publish("imports", TrackAdded(title="Giant Steps"))
    fourth, _ = await publish("imports", TrackAdded(title="secret"))

    body = {"cursors": {"imports": first}}
    if transport == "rest":
        peer = None
        answer = (await application.rest("/events/poll", body, **{"x-user": "jure"})).json()
    else:
        peer = FakePeer()
        answer = (await application.command("/events/poll", body, peer, **{"x-user": "jure"})).payload

    assert answer["topics"] == {"imports": {"rev": fourth, "events": [polled(third, first, "Giant Steps")]}}
    assert seen == [(peer, "jure", "imports", "imports", rev) for rev in (second, third, fourth)]


@pytest.mark.asyncio
async def test_a_push_after_a_refused_one_and_its_gap_poll_do_not_deliver_the_refused_event(application):
    settings.viewsets_event_authorizers = [allow_all]
    settings.viewsets_event_delivery_filters = [refuse_secrets()]
    peer = FakePeer()
    held = (await application.command("/events/subscribe", {"topic": "imports"}, peer)).payload["rev"]

    secret, _ = await publish("imports", TrackAdded(title="secret"))
    later, prev = await publish("imports", TrackAdded(title="Naima"))
    await wait_for_pushes()
    assert prev == secret
    assert [(payload, headers[":rev"], headers[":prev"]) for payload, headers in peer.pushes] == [
        ({"title": "Naima"}, later, secret)
    ]

    stream = await application.command("/events/poll", {"cursors": {"imports": held}}, peer)
    assert stream.payload["topics"] == {"imports": {"rev": later, "events": [polled(later, held, "Naima")]}}


@pytest.mark.asyncio
async def test_a_raising_delivery_filter_counts_as_true_on_poll(application):
    settings.viewsets_event_authorizers = [allow_all]

    def broken(*_):
        raise RuntimeError("broken")

    settings.viewsets_event_delivery_filters = [broken]
    first, _ = await publish("imports", TrackAdded(title="Naima"))
    second, _ = await publish("imports", TrackAdded(title="secret"))

    response = await application.rest("/events/poll", {"cursors": {"imports": 0}})
    assert response.json()["topics"] == {
        "imports": {"rev": second, "events": [polled(first, 0, "Naima"), polled(second, first, "secret")]}
    }


@pytest.mark.asyncio
async def test_poll_without_delivery_filters_answers_every_event_with_its_prev(application):
    settings.viewsets_event_authorizers = [allow_all]
    first, _ = await publish("imports", TrackAdded(title="Naima"))
    second, _ = await publish("imports", TrackAdded(title="secret"))
    third, _ = await publish("imports", TrackAdded(title="Giant Steps"))

    response = await application.rest("/events/poll", {"cursors": {"imports": first}})
    assert response.json()["topics"] == {
        "imports": {"rev": third, "events": [polled(second, first, "secret"), polled(third, second, "Giant Steps")]}
    }


@pytest.mark.asyncio
async def test_poll_from_the_current_rev_answers_no_events(application):
    settings.viewsets_event_authorizers = [allow_all]
    rev, _ = await publish("imports", TrackAdded(title="Naima"))
    response = await application.rest("/events/poll", {"cursors": {"imports": rev}})
    assert response.json()["topics"] == {"imports": {"rev": rev, "events": []}}


@pytest.mark.asyncio
async def test_poll_answers_null_events_when_the_log_cannot_answer(application):
    settings.viewsets_event_authorizers = [allow_all]
    settings.viewsets_event_log_size = 0
    first, _ = await publish("imports", TrackAdded(title="Naima"))
    second, _ = await publish("imports", TrackAdded(title="Giant Steps"))

    response = await application.rest("/events/poll", {"cursors": {"imports": first}})
    assert response.json()["topics"] == {"imports": {"rev": second, "events": None}}


@pytest.mark.asyncio
async def test_poll_refuses_each_topic_with_its_own_code(application):
    async def admin_only(_request, _cls, context):
        return await context.user == "admin"

    open_router = APIRouter()

    @route_viewset(open_router, base_path="/open", pk_field_name="id")
    class OpenViewSet(CollectionViewSet[int, Track], ViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container={}, pk_field="id")

    application.app.include_router(open_router)
    settings.viewsets_command_middleware = [Session(), Authorization()]
    settings.viewsets_event_authorizers = [ViewsetTopics()]
    action_configuration({Session: False})(application.events)
    action_configuration({Session: False})(OpenViewSet)
    action_configuration({Authorization: admin_only})(application.music)
    body = {"cursors": {"/api/music": None, "/open": None}}

    anonymous = (await application.rest("/events/poll", body)).json()["topics"]
    assert anonymous == {
        "/api/music": {"rev": None, "events": None, "error": "session_expired", "status": 401},
        "/open": {"rev": 0, "events": []},
    }
    member = (await application.rest("/events/poll", body, **{"x-user": "jure"})).json()["topics"]
    assert member["/api/music"] == {"rev": None, "events": None, "error": "not_authorized", "status": 403}
    admin = (await application.rest("/events/poll", body, **{"x-user": "admin"})).json()["topics"]
    assert admin["/api/music"] == {"rev": 0, "events": []}


@pytest.mark.asyncio
async def test_poll_counts_one_call_of_the_viewset_action_per_topic(application):
    limiter = RateLimiter(default_limit=1)
    settings.viewsets_command_middleware = [limiter]
    settings.viewsets_event_authorizers = [ViewsetTopics()]
    action_configuration({RateLimiter: 100})(application.events)
    body = {"cursors": {"/api/music": None}}

    assert (await application.rest("/events/poll", body)).json()["topics"]["/api/music"]["rev"] == 0
    second = (await application.rest("/events/poll", body)).json()["topics"]["/api/music"]
    assert second == {"rev": None, "events": None, "error": "rate_limited", "status": 429}


@pytest.mark.asyncio
async def test_poll_carries_at_most_the_subscription_limit_of_cursors(application):
    settings.viewsets_event_authorizers = [allow_all]
    settings.viewsets_event_max_subscriptions = 2

    response = await application.rest("/events/poll", {"cursors": {"a": None, "b": None, "c": None}})
    assert response.status_code == 422
    assert response.json()["detail_code"] == "event_cursors_exceeded"
    assert response.json()["detail_params"] == {"count": 3, "limit": 2}

    response = await application.rest("/events/poll", {"cursors": {"a": None, "b": None}})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_register_events_mounts_one_viewset_per_call():
    first_router, second_router = APIRouter(), APIRouter()
    first = register_events(first_router)
    second = register_events(second_router, base_path="/live")
    assert first is not second
    assert first.__viewset_metadata__["base_path"] == "/events"
    assert second.__viewset_metadata__["base_path"] == "/live"
    assert {"subscribe", "unsubscribe", "poll"} <= set(second.__viewset_metadata__["paths"])

    settings.viewsets_event_authorizers = [allow_all]
    app = FastAPI()
    app.include_router(second_router, prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/live/poll", json={"cursors": {"imports": None}})
    assert response.json() == {"topics": {"imports": {"rev": 0, "events": []}}, "poll_after_ms": 0}


@pytest.mark.asyncio
async def test_the_events_viewset_has_a_schema_on_each_transport(application):
    stream = FakeStream({":method": "GET", ":path": "/events/schema"})
    assert await process_command(None, stream, app=application.app) is True
    assert stream.status == 200
    assert {"/events/subscribe", "/events/unsubscribe", "/events/poll"} <= set(stream.payload["paths"])

    async with AsyncClient(transport=ASGITransport(app=application.app), base_url="http://test") as client:
        document = (await client.get("/events/schema")).json()
    assert "/events/poll" in document["paths"]
    assert "/events/subscribe" not in document["paths"]
    answer = document["components"]["schemas"]["TopicAnswer"]
    assert set(answer["properties"]) == {"rev", "events", "error", "status", "scope"}
    assert answer["required"] == ["rev", "events"]
    polled = document["components"]["schemas"]["PolledEvent"]
    assert set(polled["properties"]) == {"rev", "prev", "event", "version", "payload"}
    assert polled["properties"]["version"]["type"] == "integer"
    assert polled["properties"]["version"]["minimum"] == 1
    assert "event_version" in polled["properties"]["version"]["description"]
    assert polled["required"] == ["rev", "prev", "event", "version"]


@pytest.mark.asyncio
async def test_unsubscribe_without_peer_raises_naming_the_argument(application, caplog):
    with caplog.at_level(logging.ERROR):
        stream = await application.command("/events/unsubscribe", {"topic": "/api/music"})
    assert stream.status == 500
    assert "unsubscribe needs the socket" in caplog.text
    assert "peer=" in caplog.text


@pytest.mark.asyncio
async def test_a_subscribe_whose_socket_closes_meanwhile_leaves_no_subscription(application):
    peer = FakePeer()

    async def closing_meanwhile(_request, _context, _topic):
        peer.close()
        return True

    settings.viewsets_event_authorizers = [closing_meanwhile]
    stream = await application.command("/events/subscribe", {"topic": "imports:7"}, peer)
    assert stream.status == 200
    assert subscriptions("imports:7") == []
    assert topics_of(peer) == []


@pytest.mark.asyncio
async def test_a_repeated_subscribe_answered_by_idempotency_does_not_subscribe_its_socket(application):
    settings.viewsets_command_middleware = [Idempotency()]
    settings.viewsets_event_authorizers = [allow_all]
    first, second = FakePeer(), FakePeer()
    key = {"idempotency-key": '"subscribe-1"'}

    assert (await application.command("/events/subscribe", {"topic": "imports"}, first, **key)).payload == {"rev": 0}
    repeated = await application.command("/events/subscribe", {"topic": "imports"}, second, **key)
    assert (repeated.status, repeated.payload) == (200, {"rev": 0})
    assert topics_of(first) == ["imports"]
    assert topics_of(second) == []


@pytest.mark.asyncio
async def test_poll_answers_a_starlette_http_exception_of_an_authorizer_in_its_topic(application):
    async def refusing_orgs(_request, _context, topic):
        if topic.startswith("orgs"):
            raise StarletteHTTPException(403)
        return True

    settings.viewsets_event_authorizers = [refusing_orgs]
    response = await application.rest("/events/poll", {"cursors": {"imports": None, "orgs:9": None}})
    assert response.status_code == 200
    assert response.json()["topics"] == {
        "imports": {"rev": 0, "events": []},
        "orgs:9": {"rev": None, "events": None, "error": "event_topic_refused", "status": 403},
    }


# ViewsetTopics against the route's dependencies and path parameters


async def require_admin(x_user: str | None = Header(default=None)) -> None:
    if x_user != "admin":
        raise HTTPException(403, "admins only")


def secrets_application(**include_kwargs) -> Application:
    application = Application()
    router = APIRouter()

    @route_viewset(router, base_path="/secrets", pk_field_name="id")
    class SecretsViewSet(CollectionViewSet[int, Track], ViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container={}, pk_field="id")

    application.app.include_router(router, **include_kwargs)
    return application


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "include_kwargs",
    [
        pytest.param({"prefix": "/admin", "dependencies": [Depends(require_admin)]}, id="include_router"),
        pytest.param({"prefix": "/admin"}, id="no-dependency"),
    ],
)
async def test_viewset_topics_runs_the_dependencies_of_include_router(include_kwargs):
    application = secrets_application(**include_kwargs)
    settings.viewsets_event_authorizers = [ViewsetTopics()]
    guarded = "dependencies" in include_kwargs
    body = {"cursors": {"/admin/secrets": None}}

    stream = await application.command("/events/subscribe", {"topic": "/admin/secrets"}, FakePeer())
    assert stream.status == (403 if guarded else 200)
    member = (await application.rest("/events/poll", body)).json()["topics"]["/admin/secrets"]
    assert member == (
        {"rev": None, "events": None, "error": "event_topic_refused", "status": 403}
        if guarded
        else {"rev": 0, "events": []}
    )
    admin = (await application.rest("/events/poll", body, **{"x-user": "admin"})).json()["topics"]["/admin/secrets"]
    assert admin == {"rev": 0, "events": []}


@pytest.mark.asyncio
async def test_viewset_topics_refuses_a_topic_whose_dependency_parameters_do_not_validate():
    async def org_exists(org_id: int = Path()) -> int:
        return org_id

    application = secrets_application(prefix="/orgs/{org_id}", dependencies=[Depends(org_exists)])
    settings.viewsets_event_authorizers = [ViewsetTopics(), allow_all]
    body = {"cursors": {"/orgs/7/secrets": None, "/orgs/seven/secrets": None}}
    topics = (await application.rest("/events/poll", body)).json()["topics"]
    assert topics == {
        "/orgs/7/secrets": {"rev": 0, "events": []},
        "/orgs/seven/secrets": {"rev": None, "events": None, "error": "event_topic_refused", "status": 403},
    }


@pytest.mark.asyncio
async def test_a_viewset_under_a_parametrised_prefix_is_followed_on_the_path_with_its_values():
    members = {"7": {"ana"}, "9": {"bor"}}

    async def org_member(request, _cls, context):
        return await context.user in members[request.path_params["org_id"]]

    application = Application()
    router = APIRouter()

    @route_viewset(router, base_path="/tracks", pk_field_name="id")
    class OrgTracksViewSet(CollectionViewSet[int, Track], ViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container={1: Track(id=1, title="Blue Train")}, pk_field="id")

    application.app.include_router(router, prefix="/orgs/{org_id}")
    settings.viewsets_command_middleware = [Authorization(), ChangeEvents()]
    settings.viewsets_event_authorizers = [ViewsetTopics(), allow_authenticated]
    action_configuration({Authorization: org_member})(OrgTracksViewSet)
    action_configuration({Authorization: False})(application.events)

    peer = FakePeer()
    stream = await application.command("/events/subscribe", {"topic": "/orgs/7/tracks"}, peer, **{"x-user": "ana"})
    assert stream.status == 200
    refused = await application.command(
        "/events/subscribe", {"topic": "/orgs/7/tracks"}, FakePeer(), **{"x-user": "bor"}
    )
    assert refused.status == 403
    assert refused.payload["detail_code"] == "not_authorized"

    deleted = await application.rest("/orgs/7/tracks/1", None, method="DELETE", **{"x-user": "ana"})
    assert deleted.status_code == 200
    await wait_for_pushes()
    assert [headers[":topic"] for _payload, headers in peer.pushes] == ["/orgs/7/tracks"]

    body = {"cursors": {"/orgs/7/tracks": 0}}
    outsider = (await application.rest("/events/poll", body, **{"x-user": "bor"})).json()["topics"]
    assert outsider["/orgs/7/tracks"]["error"] == "not_authorized"
    member = (await application.rest("/events/poll", body, **{"x-user": "ana"})).json()["topics"]
    assert [event["payload"]["op"] for event in member["/orgs/7/tracks"]["events"]] == ["deleted"]
