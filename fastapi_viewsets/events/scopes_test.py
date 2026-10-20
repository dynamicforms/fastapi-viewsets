import logging

from typing import Any

import pytest

from fastapi import APIRouter, FastAPI, HTTPException, Request
from httpx2 import ASGITransport, AsyncClient
from pydantic import BaseModel, Field

from ..action_configuration import action_configuration
from ..collection_viewset import CollectionViewSet
from ..conf import settings
from ..context import Context
from ..decorators import route_viewset
from ..exceptions import df_viewset_exception_handler, DfViewSetError
from ..middleware import Middleware, ViewSetResult
from ..middleware.idempotency import Idempotency
from ..middleware.rate_limiter import RateLimiter
from ..mixins import ViewSetMixin
from ..mux_ws.registry import reset_registry
from ..mux_ws.server import process_command
from . import scopes as scopes_module
from .authorizers import allow_all, allow_authenticated, DeclaredSources, ViewsetTopics
from .changes import Change, ChangeEvents, RecordsChanged, write_topic
from .conftest import FakePeer
from .delivery import subscriptions, topics_of, wait_for_pushes
from .scopes import (
    event_scope,
    EventScope,
    GATE_RUN_SCOPE_KEY,
    reset_event_scopes,
    resolve_scope,
    resolve_topic,
    SCOPE_CONTEXT_KEY,
    scope_of,
)
from .sources import publish, source
from .viewset import register_events
from .viewset_test import FakeStream, TrackAdded

# scope declaration


@pytest.fixture(autouse=True)
def _reset_scopes():
    reset_event_scopes()
    yield
    reset_event_scopes()


def test_event_scope_names_the_class_by_default_and_subclasses_inherit_it():
    @event_scope(lambda _request, _context: "1")
    class Parent:
        pass

    class Child(Parent):
        pass

    scope = scope_of(Parent)
    assert scope.name == f"{__name__}.{Parent.__qualname__}"
    assert scope_of(Child) is scope
    assert scope_of(object) is None
    assert scope.topic(7) == f"@{scope.name}:7"


@pytest.mark.parametrize("name", ["", "a b", "a:b", "tab\there"])
def test_event_scope_refuses_a_name_that_is_not_a_scope_name(name):
    with pytest.raises(ValueError, match="not a scope name"):
        event_scope(lambda _request, _context: "1", name=name)(type("Holder", (), {}))


def test_a_scope_name_belongs_to_one_class_and_its_subclasses():
    @event_scope(lambda _request, _context: "1", name="shared")
    class Owner:
        pass

    @event_scope(lambda _request, _context: "2", name="shared")
    class Narrower(Owner):
        pass

    assert scope_of(Narrower).name == "shared"
    with pytest.raises(ValueError, match="shared"):

        @event_scope(lambda _request, _context: "3", name="shared")
        class Stranger:
            pass

    event_scope(lambda _request, _context: "1", name="shared")(Owner)


def test_a_reloaded_class_takes_its_scope_name_over():
    first = type("Reloaded", (), {"__module__": "reloaded.module"})
    second = type("Reloaded", (), {"__module__": "reloaded.module"})
    event_scope(lambda _request, _context: "1", name="reloaded")(first)
    event_scope(lambda _request, _context: "1", name="reloaded")(second)
    assert scopes_module._names["reloaded"] is second
    event_scope(lambda _request, _context: "1")(first)
    event_scope(lambda _request, _context: "1")(second)
    assert scope_of(second).name == "reloaded.module.Reloaded"


def test_classes_a_function_defines_need_a_scope_name_each():
    def make(name: str | None = None) -> type:
        @event_scope(lambda _request, _context: 7, name=name)
        class VS:
            pass

        return VS

    first = make()
    with pytest.raises(ValueError, match=r"needs event_scope\(name=\.\.\.\)"):
        make()
    assert scopes_module._names[scope_of(first).name] is first
    tags, members = make("tags"), make("members")
    assert scope_of(tags).topic(7) != scope_of(members).topic(7)


@pytest.mark.asyncio
async def test_resolve_scope_awaits_and_normalises_the_key():
    context = Context({})

    async def asynchronous(_request, _context):
        return 7

    assert await resolve_scope(EventScope("s", asynchronous), None, context) == "7"
    assert await resolve_scope(EventScope("s", lambda _r, _c: "seven"), None, context) == "seven"
    assert await resolve_scope(EventScope("s", lambda _r, _c: None), None, context) is None
    for key, error in [(True, TypeError), (7.0, TypeError), ("", ValueError), ("a b", ValueError)]:
        with pytest.raises(error):
            await resolve_scope(EventScope("s", lambda _r, _c, key=key: key), None, context)


# the application: a tags viewset on a header-scoped route and on a path-scoped route

MEMBERS = {"ana": {"7"}, "bor": {"8"}, "root": {"7", "8"}}


class Tag(BaseModel):
    id: int = Field(json_schema_extra={"autoinc_int": True})
    title: str


class SelectedOrg(Middleware):
    """Selects the organisation of the call from the path parameter `org_slug`, else from the `x-org`
    header, and admits a member of it only; records every call and every gate run. Configured False,
    it does nothing."""

    def __init__(self):
        self.calls: list[str | None] = []
        self.gate_scopes: list[dict[str, Any]] = []

    async def __call__(self, request, _viewset, context, call_next):
        if request is not None and self.config_from(context) is not False:
            if request.scope.get(GATE_RUN_SCOPE_KEY):
                self.gate_scopes.append(request.scope)
            slug = request.path_params.get("org_slug") or request.headers.get("x-org")
            self.calls.append(slug)
            if slug is not None:
                if slug not in MEMBERS.get(request.headers.get("x-user"), set()):
                    raise HTTPException(403, "not a member of the organisation")
                context.set("org_id", slug)
        return await call_next()


async def org_scope(_request: Request | None, context: Context) -> str | None:
    return context.raw().get("org_id")


class Scoped:
    """`TagsViewSet` at `/api/v1/tags` (organisation from `x-org`), `OrgTagsViewSet`, its subclass, at
    `/orgs/{org_slug}/tags`, the events viewset at `/events`."""

    def __init__(self, resolver=org_scope, middleware: list | None = None):
        self.selected = SelectedOrg()
        settings.viewsets_command_middleware = middleware if middleware is not None else [self.selected, ChangeEvents()]
        settings.viewsets_event_authorizers = [ViewsetTopics()]
        self.records: dict[int, Tag] = {}
        records = self.records
        api, orgs, events = APIRouter(), APIRouter(prefix="/orgs/{org_slug}"), APIRouter()

        @event_scope(resolver, name="tags")
        @route_viewset(api, "/tags", pk_field_name="id")
        class TagsViewSet(CollectionViewSet[int, Tag], ViewSetMixin[int, Tag]):
            def __init__(self):
                super().__init__(container=records, pk_field="id")

        class OrgTagsViewSet(TagsViewSet):
            pass

        route_viewset(orgs, "/tags", pk_field_name="id")(OrgTagsViewSet)
        self.tags, self.org_tags = TagsViewSet, OrgTagsViewSet
        self.events = action_configuration({SelectedOrg: False})(register_events(events))
        self.app = FastAPI()
        self.app.add_exception_handler(DfViewSetError, df_viewset_exception_handler)
        self.app.include_router(api, prefix="/api/v1")
        self.app.include_router(orgs)
        self.app.include_router(events)

    async def command(self, path: str, payload: Any, peer: Any = None, method: str = "POST", **headers):
        stream = FakeStream({":method": method, ":path": path, **headers})
        assert await process_command(payload, stream, app=self.app, peer=peer) is True
        return stream

    async def rest(self, path: str, payload: Any, method: str = "POST", **headers: str):
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as client:
            return await client.request(method, path, json=payload, headers=headers)

    async def subscribe(self, topic: str, peer: Any, **headers: str) -> FakeStream:
        return await self.command("/events/subscribe", {"topic": topic}, peer, **headers)

    async def poll(self, body: dict[str, Any], **headers: str) -> dict[str, Any]:
        response = await self.rest("/events/poll", body, **headers)
        assert response.status_code == 200, response.text
        return response.json()["topics"]


@pytest.fixture(autouse=True)
def _reset_application_settings():
    saved = settings.viewsets_command_middleware, settings.viewsets_context_processors
    saved_reauthorize = settings.viewsets_event_reauthorize_after
    settings.viewsets_context_processors = []
    reset_registry()
    yield
    settings.viewsets_command_middleware, settings.viewsets_context_processors = saved
    settings.viewsets_event_reauthorize_after = saved_reauthorize
    reset_registry()


def user(name: str, org: str | None = None) -> dict[str, str]:
    return {"x-user": name, **({"x-org": org} if org is not None else {})}


def pushed(peer: FakePeer) -> list[tuple[str, str | None, Any]]:
    return [(headers[":topic"], headers.get(":scope"), payload["op"]) for payload, headers in peer.pushes]


# mixed routes


@pytest.mark.asyncio
async def test_a_write_on_either_route_reaches_every_form_of_its_scope_and_no_other_scope():
    app = Scoped()
    by_header, by_path, other = FakePeer(), FakePeer(), FakePeer()

    stream = await app.subscribe("/api/v1/tags", by_header, **user("ana", "7"))
    assert (stream.status, stream.payload) == (200, {"rev": 0, "scope": "7"})
    stream = await app.subscribe("/orgs/7/tags", by_path, **user("ana"))
    assert (stream.status, stream.payload) == (200, {"rev": 0, "scope": "7"})
    assert (await app.subscribe("/orgs/8/tags", other, **user("bor"))).payload == {"rev": 0, "scope": "8"}
    assert {s.peer for s in subscriptions("@tags:7")} == {by_header, by_path}

    assert (await app.rest("/api/v1/tags", {"title": "jazz"}, **user("ana", "7"))).status_code == 200
    await wait_for_pushes()
    assert pushed(by_header) == [("/api/v1/tags", "7", "created")]
    assert pushed(by_path) == [("/orgs/7/tags", "7", "created")]
    assert other.pushes == []

    stream = await app.command("/orgs/7/tags", {"title": "blues"}, None, **user("ana"))
    assert stream.status == 200
    await wait_for_pushes()
    assert [entry[0] for entry in pushed(by_header)] == ["/api/v1/tags", "/api/v1/tags"]
    assert [entry[0] for entry in pushed(by_path)] == ["/orgs/7/tags", "/orgs/7/tags"]
    assert other.pushes == []
    [_first, (_payload, headers)] = by_header.pushes
    assert headers[":prev"] == by_header.pushes[0][1][":rev"]

    assert (await app.rest("/orgs/8/tags", {"title": "rock"}, **user("bor"))).status_code == 200
    await wait_for_pushes()
    assert pushed(other) == [("/orgs/8/tags", "8", "created")]
    assert len(by_header.pushes) == len(by_path.pushes) == 2


@pytest.mark.asyncio
async def test_a_scope_the_caller_may_not_read_is_refused():
    app = Scoped()
    peer = FakePeer()
    stream = await app.subscribe("/orgs/8/tags", peer, **user("ana"))
    assert stream.status == 403
    stream = await app.subscribe("/api/v1/tags", peer, **user("ana", "8"))
    assert stream.status == 403
    assert topics_of(peer) == []

    stream = await app.subscribe("/api/v1/tags", peer, **user("ana"))
    assert stream.status == 403
    assert stream.payload["detail_code"] == "fastapi-viewsets.event_scope_missing"
    assert stream.payload["detail_params"] == {"topic": "/api/v1/tags"}


@pytest.mark.asyncio
async def test_a_client_cannot_name_a_store_topic():
    app = Scoped()
    stream = await app.subscribe("@tags:7", FakePeer(), **user("ana"))
    assert stream.status == 422
    assert stream.payload["detail_code"] == "fastapi-viewsets.event_topic_invalid"
    topics = await app.poll({"cursors": {"@tags:7": None}}, **user("ana"))
    assert topics == {
        "@tags:7": {"rev": None, "events": None, "error": "fastapi-viewsets.event_topic_invalid", "status": 422}
    }


@pytest.mark.asyncio
async def test_poll_answers_both_forms_from_the_scope_s_store_topic():
    app = Scoped()
    first = await app.poll({"cursors": {"/api/v1/tags": None, "/orgs/7/tags": None}}, **user("ana", "7"))
    assert first == {
        "/api/v1/tags": {"rev": 0, "events": [], "scope": "7"},
        "/orgs/7/tags": {"rev": 0, "events": [], "scope": "7"},
    }
    await app.rest("/orgs/7/tags", {"title": "jazz"}, **user("ana"))
    body = {"cursors": {"/api/v1/tags": 0, "/orgs/7/tags": 0}, "scopes": {"/api/v1/tags": "7", "/orgs/7/tags": "7"}}
    topics = await app.poll(body, **user("ana", "7"))
    for topic in ("/api/v1/tags", "/orgs/7/tags"):
        [event] = topics[topic]["events"]
        assert event["payload"]["op"] == "created"
        assert topics[topic]["scope"] == "7"
        assert topics[topic]["rev"] == event["rev"]


@pytest.mark.asyncio
async def test_poll_resyncs_a_cursor_held_under_another_scope():
    app = Scoped()
    await app.rest("/orgs/7/tags", {"title": "jazz"}, **user("root"))
    [rev] = [answer["rev"] for answer in (await app.poll({"cursors": {"/orgs/7/tags": None}}, **user("root"))).values()]

    moved = await app.poll({"cursors": {"/api/v1/tags": 0}, "scopes": {"/api/v1/tags": "7"}}, **user("root", "8"))
    assert moved == {"/api/v1/tags": {"rev": 0, "events": None, "scope": "8"}}
    missing = await app.poll({"cursors": {"/api/v1/tags": 0}, "scopes": {}}, **user("root", "7"))
    assert missing == {"/api/v1/tags": {"rev": rev, "events": None, "scope": "7"}}
    trusted = await app.poll({"cursors": {"/api/v1/tags": 0}}, **user("root", "7"))
    assert [event["rev"] for event in trusted["/api/v1/tags"]["events"]] == [rev]


@pytest.mark.asyncio
async def test_poll_resyncs_an_unscoped_topic_the_client_holds_a_scope_for():
    app = Scoped()
    settings.viewsets_event_authorizers = [ViewsetTopics(), allow_all]
    rev, _prev = await publish("imports", TrackAdded(title="x"))
    answer = await app.poll({"cursors": {"imports": 0}, "scopes": {"imports": "7"}})
    assert answer == {"imports": {"rev": rev, "events": None}}


@pytest.mark.asyncio
async def test_the_unscoped_success_answers_keep_their_wire_form():
    app = Scoped()
    settings.viewsets_event_authorizers = [ViewsetTopics(), allow_all]
    peer = FakePeer()
    assert (await app.subscribe("imports", peer)).payload == {"rev": 0}
    rev, prev = await publish("imports", TrackAdded(title="x"))
    response = await app.rest("/events/poll", {"cursors": {"imports": 0}, "scopes": {}})
    assert response.json() == {
        "topics": {
            "imports": {
                "rev": rev,
                "events": [{"rev": rev, "prev": 0, "event": "track_added", "version": 1, "payload": {"title": "x"}}],
            }
        },
        "poll_after_ms": 0,
    }
    await wait_for_pushes()
    assert peer.pushes[0][1] == {
        ":event": "track_added",
        ":version": 1,
        ":topic": "imports",
        ":rev": rev,
        ":prev": prev,
    }


@pytest.mark.asyncio
async def test_delivery_filters_receive_the_client_topic_and_the_scope():
    app = Scoped()
    seen: list[tuple[str, str, str | None, str | None]] = []

    def record(subscription, topic, _rev, _name, _payload, _version):
        seen.append(("push" if subscription.peer else "poll", topic, subscription.scope, subscription.store_topic))
        return True

    settings.viewsets_event_delivery_filters = [record]
    await app.subscribe("/orgs/7/tags", FakePeer(), **user("ana"))
    await app.rest("/api/v1/tags", {"title": "jazz"}, **user("ana", "7"))
    await wait_for_pushes()
    await app.poll({"cursors": {"/api/v1/tags": 0}, "scopes": {"/api/v1/tags": "7"}}, **user("ana", "7"))
    assert seen == [("push", "/orgs/7/tags", "7", "@tags:7"), ("poll", "/api/v1/tags", "7", "@tags:7")]


# the gate run


@pytest.mark.asyncio
async def test_a_refusal_in_call_a_short_circuit_and_a_missing_key_refuse_the_topic():
    class ShortCircuit(Middleware):
        async def __call__(self, request, _viewset, _context, call_next):
            if request is not None and request.scope.get(GATE_RUN_SCOPE_KEY) and request.headers.get("x-blocked"):
                return ViewSetResult(body=None, status_code=403)
            return await call_next()

    app = Scoped(middleware=[ShortCircuit(), SelectedOrg()])
    blocked = await app.poll({"cursors": {"/orgs/7/tags": None}}, **user("ana"), **{"x-blocked": "1"})
    assert blocked["/orgs/7/tags"] == {
        "rev": None,
        "events": None,
        "error": "fastapi-viewsets.event_topic_refused",
        "status": 403,
    }
    stranger = await app.poll({"cursors": {"/orgs/7/tags": None}}, **user("bor"))
    assert stranger["/orgs/7/tags"] == {
        "rev": None,
        "events": None,
        "error": "fastapi-viewsets.event_topic_refused",
        "status": 403,
    }
    nobody = await app.poll({"cursors": {"/api/v1/tags": None}}, **user("ana"))
    assert nobody["/api/v1/tags"] == {
        "rev": None,
        "events": None,
        "error": "fastapi-viewsets.event_scope_missing",
        "status": 403,
    }


@pytest.mark.asyncio
async def test_poll_answers_the_status_of_a_resolver_s_refusal():
    def unavailable(_request, _context):
        raise HTTPException(503, "the membership service is unavailable")

    app = Scoped(resolver=unavailable)
    topics = await app.poll({"cursors": {"/orgs/7/tags": None}}, **user("ana"))
    assert topics["/orgs/7/tags"] == {
        "rev": None,
        "events": None,
        "error": "fastapi-viewsets.event_topic_refused",
        "status": 503,
    }


@pytest.mark.asyncio
async def test_the_gate_run_is_a_get_of_the_gate_action_with_a_context_of_its_own():
    seen: list[dict[str, Any]] = []

    async def processor(request, viewset):
        seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "path_params": dict(request.path_params),
                "user": request.headers.get("x-user"),
                "gate": request.scope.get(GATE_RUN_SCOPE_KEY),
                "viewset": viewset,
            }
        )
        return {"processed": request.scope.get(GATE_RUN_SCOPE_KEY) or "events"}

    app = Scoped()
    settings.viewsets_context_processors = [processor]
    events_context: list[Context] = []

    async def capture(_request, context, _topic):
        events_context.append(context)

    settings.viewsets_event_authorizers = [capture, ViewsetTopics()]
    stream = FakeStream({":method": "POST", ":path": "/events/subscribe", **user("ana")})
    assert await process_command({"topic": "/orgs/7/tags"}, stream, app=app.app, peer=FakePeer()) is True
    assert stream.status == 200, stream.payload
    events_call, gate = seen
    assert events_call["gate"] is None
    assert gate == {
        "method": "GET",
        "path": "/orgs/7/tags",
        "path_params": {"org_slug": "7"},
        "user": "ana",
        "gate": "list_items",
        "viewset": app.org_tags,
    }
    [context] = events_context
    assert context.raw().get("processed") == "events"
    assert "org_id" not in context.raw()


@pytest.mark.asyncio
async def test_the_gate_run_sees_the_lifespan_state():
    seen = []

    async def processor(request, _viewset):
        seen.append(request.state.lifespan_value)
        return {}

    app = Scoped()
    settings.viewsets_context_processors = [processor]

    async def server(scope, receive, send):
        # what an ASGI server does with the state the lifespan set: a copy per request
        scope["state"] = {"lifespan_value": "set at startup"}
        await app.app(scope, receive, send)

    async with AsyncClient(transport=ASGITransport(app=server), base_url="http://test") as client:
        response = await client.post("/events/poll", json={"cursors": {"/orgs/7/tags": None}}, headers=user("ana"))
    assert response.json()["topics"]["/orgs/7/tags"]["scope"] == "7"
    assert seen == ["set at startup", "set at startup"]


@pytest.mark.asyncio
async def test_one_gate_run_answers_every_topic_of_a_request_with_the_same_route_and_parameters():
    limiter = RateLimiter(default_limit=1)
    app = Scoped()
    settings.viewsets_command_middleware = [limiter, app.selected, ChangeEvents()]
    action_configuration({RateLimiter: 100})(app.events)
    first = await app.poll({"cursors": {"/orgs/7/tags": None}}, **user("ana"))
    assert first["/orgs/7/tags"] == {"rev": 0, "events": [], "scope": "7"}
    assert len(app.selected.gate_scopes) == 1

    second = await app.poll({"cursors": {"/orgs/7/tags": None}}, **user("ana"))
    assert second["/orgs/7/tags"] == {
        "rev": None,
        "events": None,
        "error": "fastapi-viewsets.rate_limited",
        "status": 429,
    }


@pytest.mark.asyncio
async def test_two_scoped_classes_on_parameterless_routes_get_a_gate_run_each():
    app = Scoped()
    other_router = APIRouter()

    @event_scope(lambda _request, _context: "everyone", name="notes")
    @route_viewset(other_router, "/notes", pk_field_name="id")
    class NotesViewSet(CollectionViewSet[int, Tag], ViewSetMixin[int, Tag]):
        def __init__(self):
            super().__init__(container={}, pk_field="id")

    app.app.include_router(other_router, prefix="/api/v1")
    topics = await app.poll({"cursors": {"/api/v1/tags": None, "/api/v1/notes": None}}, **user("ana", "7"))
    assert topics["/api/v1/tags"]["scope"] == "7"
    assert topics["/api/v1/notes"]["scope"] == "everyone"


@pytest.mark.asyncio
async def test_an_admitting_authorizer_does_not_skip_the_gate_run():
    app = Scoped()
    settings.viewsets_context_processors = [lambda request, _viewset: _user_context(request)]
    settings.viewsets_event_authorizers = [allow_authenticated]
    stream = await app.subscribe("/orgs/8/tags", FakePeer(), **user("ana"))
    assert stream.status == 403
    assert (await app.subscribe("/orgs/7/tags", FakePeer(), **user("ana"))).payload == {"rev": 0, "scope": "7"}


async def _user_context(request):
    return {"user": request.headers.get("x-user")}


@pytest.mark.asyncio
async def test_the_gate_run_s_route_is_the_route_of_a_real_request():
    app = Scoped()
    real: list[Any] = []

    class Capture(Middleware):
        async def __call__(self, request, _viewset, _context, call_next):
            if request is not None and not request.scope.get(GATE_RUN_SCOPE_KEY):
                real.append(request.scope.get("route"))
            return await call_next()

    settings.viewsets_command_middleware = [Capture(), app.selected, ChangeEvents()]
    await app.rest("/orgs/7/tags", None, method="GET", **user("ana"))
    await app.poll({"cursors": {"/orgs/7/tags": None}}, **user("ana"))
    [route, _events_route] = real
    [gate_scope] = app.selected.gate_scopes
    assert gate_scope["route"] is route
    assert gate_scope[GATE_RUN_SCOPE_KEY] == "list_items"


@pytest.mark.asyncio
async def test_an_idempotency_key_on_a_poll_does_not_reach_the_gate_run():
    app = Scoped()
    settings.viewsets_command_middleware = [Idempotency(), app.selected, ChangeEvents()]
    headers = {**user("ana"), "Idempotency-Key": '"poll-1"'}
    topics = await app.poll({"cursors": {"/orgs/7/tags": None}}, **headers)
    assert topics["/orgs/7/tags"] == {"rev": 0, "events": [], "scope": "7"}


@pytest.mark.asyncio
async def test_a_refused_subscribe_removes_the_subscription_it_replaces():
    app = Scoped()
    peer = FakePeer()
    assert (await app.subscribe("/api/v1/tags", peer, **user("ana", "7"))).status == 200
    assert topics_of(peer) == ["/api/v1/tags"]
    assert (await app.subscribe("/api/v1/tags", peer, **user("ana", "8"))).status == 403
    assert topics_of(peer) == []
    assert subscriptions("@tags:7") == []


@pytest.mark.asyncio
async def test_a_re_subscribe_under_another_scope_moves_the_subscription():
    app = Scoped()
    peer = FakePeer()
    await app.subscribe("/api/v1/tags", peer, **user("root", "7"))
    assert (await app.subscribe("/api/v1/tags", peer, **user("root", "8"))).payload == {"rev": 0, "scope": "8"}
    assert subscriptions("@tags:7") == []
    assert [s.peer for s in subscriptions("@tags:8")] == [peer]


@pytest.mark.asyncio
async def test_a_poll_over_the_socket_that_refuses_a_topic_unsubscribes_it():
    app = Scoped()
    peer = FakePeer()
    await app.subscribe("/api/v1/tags", peer, **user("root", "7"))
    MEMBERS["root"].discard("7")
    try:
        stream = await app.command("/events/poll", {"cursors": {"/api/v1/tags": 0}}, peer, **user("root", "7"))
    finally:
        MEMBERS["root"].add("7")
    assert stream.payload["topics"]["/api/v1/tags"]["status"] == 403
    assert topics_of(peer) == []


class FailingGate(Middleware):
    """Raises `HTTPException(status)` in every gate run while `status` is set."""

    def __init__(self):
        self.status: int | None = None

    async def __call__(self, request, _viewset, _context, call_next):
        if self.status is not None and request is not None and request.scope.get(GATE_RUN_SCOPE_KEY):
            raise HTTPException(self.status, "the gate failed")
        return await call_next()


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "kept"), [(429, True), (500, True), (503, True), (401, False), (403, False)])
async def test_a_poll_over_the_socket_unsubscribes_a_topic_on_a_refusal_only(status, kept):
    failing = FailingGate()
    app = Scoped(middleware=[failing, SelectedOrg(), ChangeEvents()])
    peer = FakePeer()
    await app.subscribe("/orgs/7/tags", peer, **user("ana"))
    failing.status = status
    stream = await app.command("/events/poll", {"cursors": {"/orgs/7/tags": 0}}, peer, **user("ana"))
    assert stream.payload["topics"]["/orgs/7/tags"]["status"] == status
    assert topics_of(peer) == (["/orgs/7/tags"] if kept else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "kept"), [(429, True), (503, True), (403, False)])
async def test_a_poll_over_the_socket_keeps_an_unscoped_topic_an_authorizer_fails_transiently(status, kept):
    app = Scoped()
    failing: list[int] = []

    async def authorizer(_request, _context, _topic):
        if failing:
            raise HTTPException(failing[0], "the authorizer failed")
        return True

    settings.viewsets_event_authorizers = [authorizer]
    peer = FakePeer()
    assert (await app.subscribe("imports", peer)).payload == {"rev": 0}
    failing.append(status)
    stream = await app.command("/events/poll", {"cursors": {"imports": 0}}, peer)
    assert stream.payload["topics"]["imports"]["status"] == status
    assert topics_of(peer) == (["imports"] if kept else [])


@pytest.mark.asyncio
async def test_a_route_dependency_that_does_not_validate_refuses_the_topic_in_the_gate_run():
    from fastapi import Depends, Header

    def org_header(x_tenant: int = Header()) -> int:
        return x_tenant

    app = Scoped()
    router = APIRouter(dependencies=[Depends(org_header)])

    @event_scope(lambda _request, _context: "everyone", name="notes")
    @route_viewset(router, "/notes", pk_field_name="id")
    class NotesViewSet(CollectionViewSet[int, Tag], ViewSetMixin[int, Tag]):
        def __init__(self):
            super().__init__(container={}, pk_field="id")

    app.app.include_router(router)
    stream = await app.subscribe("/notes", FakePeer(), **user("ana"))
    assert stream.status == 403
    assert stream.payload["detail_code"] == "fastapi-viewsets.event_topic_refused"
    admitted = await app.subscribe("/notes", FakePeer(), **user("ana"), **{"x-tenant": "3"})
    assert admitted.payload == {"rev": 0, "scope": "everyone"}


@pytest.mark.asyncio
async def test_resolve_topic_raises_the_kept_refusal_for_a_topic_of_the_same_gate_run():
    app = Scoped()
    request = Request(
        {
            "type": "http",
            "app": app.app,
            "headers": [(b"x-user", b"bor")],
            "path": "/events/poll",
            "method": "POST",
            "query_string": b"",
        }
    )
    with pytest.raises(HTTPException) as first:
        await resolve_topic(request, "/orgs/7/tags")
    with pytest.raises(HTTPException) as second:
        await resolve_topic(request, "/orgs/7/tags")
    assert second.value is first.value
    assert len(app.selected.gate_scopes) == 1


# reauthorization


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "kept"), [(429, True), (503, True), (403, False)])
async def test_a_reauthorization_removes_the_subscription_on_a_refusal_only(status, kept):
    failing = FailingGate()
    app = Scoped(middleware=[failing, SelectedOrg(), ChangeEvents()])
    settings.viewsets_event_reauthorize_after = 0.0
    peer = FakePeer()
    await app.subscribe("/orgs/7/tags", peer, **user("ana"))
    failing.status = status
    await app.rest("/orgs/7/tags", {"title": "jazz"}, **user("ana"))
    await wait_for_pushes()
    assert peer.pushes == []
    assert topics_of(peer) == (["/orgs/7/tags"] if kept else [])
    failing.status = None
    await app.rest("/orgs/7/tags", {"title": "blues"}, **user("ana"))
    await wait_for_pushes()
    assert len(peer.pushes) == (1 if kept else 0)


@pytest.mark.asyncio
async def test_a_revoked_membership_ends_the_pushes_once_the_subscription_is_reauthorized():
    app = Scoped()
    settings.viewsets_event_reauthorize_after = 0.0
    peer = FakePeer()
    await app.subscribe("/orgs/7/tags", peer, **user("bor"))  # refused
    MEMBERS["bor"].add("7")
    try:
        assert (await app.subscribe("/orgs/7/tags", peer, **user("bor"))).status == 200
        await app.rest("/orgs/7/tags", {"title": "jazz"}, **user("ana"))
        await wait_for_pushes()
        assert pushed(peer) == [("/orgs/7/tags", "7", "created")]
    finally:
        MEMBERS["bor"].discard("7")
    await app.rest("/orgs/7/tags", {"title": "blues"}, **user("ana"))
    await wait_for_pushes()
    assert len(peer.pushes) == 1
    assert topics_of(peer) == []


# writes


@pytest.mark.asyncio
async def test_change_events_carries_the_key_resolved_in_the_web_process():
    calls: list[str] = []

    def resolver(request, context):
        calls.append(request.method)
        return context.raw().get("org_id")

    app = Scoped(resolver=resolver)
    keys: list[Any] = []

    class Spy(Middleware):
        async def __call__(self, _request, _viewset, _context, call_next):
            return await call_next()

        async def execute(self, execution, call_next):
            keys.append(execution.context.raw().get(SCOPE_CONTEXT_KEY))
            return await call_next()

    settings.viewsets_command_middleware.append(Spy())
    await app.rest("/orgs/7/tags", {"title": "jazz"}, **user("ana"))
    assert [await key for key in keys] == [{"name": "tags", "key": "7"}]
    assert calls == ["POST"]
    assert await _store_rev("@tags:7") > 0


async def _store_rev(topic: str) -> int:
    from .store import get_store

    return (await get_store().revs([topic])).get(topic, 0)


@pytest.mark.asyncio
async def test_change_events_ahead_of_the_scope_s_inputs_resolves_the_key_in_execute():
    selected = SelectedOrg()
    app = Scoped(middleware=[ChangeEvents(), selected])
    peer = FakePeer()
    await app.subscribe("/orgs/7/tags", peer, **user("ana"))
    await app.rest("/orgs/7/tags", {"title": "jazz"}, **user("ana"))
    await wait_for_pushes()
    assert pushed(peer) == [("/orgs/7/tags", "7", "created")]


@pytest.mark.asyncio
async def test_change_events_ahead_of_a_strict_resolver_s_inputs_resolves_the_key_after_the_chain():
    def strict(_request, context):
        if "org_id" not in context:
            raise HTTPException(403, "no organisation")
        return context.raw()["org_id"]

    app = Scoped(resolver=strict, middleware=[ChangeEvents(), SelectedOrg()])
    by_path, by_header = FakePeer(), FakePeer()
    await app.subscribe("/orgs/7/tags", by_path, **user("ana"))
    await app.subscribe("/api/v1/tags", by_header, **user("ana", "7"))
    assert (await app.rest("/orgs/7/tags", {"title": "jazz"}, **user("ana"))).status_code == 200
    assert (await app.rest("/api/v1/tags", {"title": "blues"}, **user("ana", "7"))).status_code == 200
    await wait_for_pushes()
    assert pushed(by_path) == [("/orgs/7/tags", "7", "created")] * 2
    assert pushed(by_header) == [("/api/v1/tags", "7", "created")] * 2


@pytest.mark.asyncio
async def test_a_custom_endpoint_publishes_to_the_write_topic_of_its_scope():
    app = Scoped()

    class ArchivingTags(app.org_tags):
        __router = APIRouter()

        @__router.post("/{pk}/archive")
        async def archive(self, request: Request, context: Context, pk: int) -> None:
            topic = await write_topic(self, request, context)
            await publish(topic, RecordsChanged(op="deleted", changes=[Change(pk=pk)]))

    archive_router = APIRouter(prefix="/archive/{org_slug}")
    route_viewset(archive_router, "/tags", pk_field_name="id")(ArchivingTags)
    app.app.include_router(archive_router)
    by_header, other = FakePeer(), FakePeer()
    await app.subscribe("/api/v1/tags", by_header, **user("ana", "7"))
    await app.subscribe("/orgs/8/tags", other, **user("bor"))
    response = await app.rest("/archive/7/tags/3/archive", None, **user("ana"))
    assert response.status_code == 200, response.text
    await wait_for_pushes()
    assert pushed(by_header) == [("/api/v1/tags", "7", "deleted")]
    assert other.pushes == []


@pytest.mark.asyncio
async def test_write_topic_of_an_unscoped_class_is_the_routed_topic():
    class Plain:
        __viewset_metadata__ = {"base_path": "/plain"}

    assert await write_topic(Plain, None, Context({"fastapi_viewsets.events.topic": "/api/plain"})) == "/api/plain"
    assert await write_topic(Plain(), None, Context({})) == "/plain"
    assert await write_topic(object, None, Context({})) is None
    assert await write_topic(type("Keyless", (), {}), None, Context({})) is None


@pytest.mark.asyncio
async def test_a_write_without_a_key_publishes_nothing_and_warns(caplog):
    app = Scoped(resolver=lambda _request, _context: None)
    with caplog.at_level(logging.WARNING):
        response = await app.rest("/orgs/7/tags", {"title": "jazz"}, **user("ana"))
    assert response.status_code == 200
    assert "'tags' answered no key" in caplog.text
    assert app.records


@pytest.mark.asyncio
async def test_a_resolver_that_raises_refuses_the_write_before_the_action():
    def refuse(request, _context):
        if request is not None and request.method == "POST":
            raise HTTPException(403, "read only")
        return "7"

    app = Scoped(resolver=refuse)
    response = await app.rest("/orgs/7/tags", {"title": "jazz"}, **user("ana"))
    assert response.status_code == 403
    assert app.records == {}


@pytest.mark.asyncio
async def test_a_topic_option_on_a_scoped_class_raises():
    app = Scoped()
    action_configuration({ChangeEvents: {"topic": "/elsewhere"}})(app.org_tags)
    with pytest.raises(TypeError, match="exclude each other"):
        await app.rest("/orgs/7/tags", {"title": "jazz"}, **user("ana"))


@pytest.mark.asyncio
async def test_an_override_of_topic_for_wins_over_the_scope():
    class Custom(ChangeEvents):
        def topic_for(self, _execution):
            return "@custom:1"

    app = Scoped(middleware=[])
    settings.viewsets_command_middleware = [app.selected, Custom()]
    await app.rest("/orgs/7/tags", {"title": "jazz"}, **user("ana"))
    assert await _store_rev("@custom:1") > 0
    assert await _store_rev("@tags:7") == 0


@pytest.mark.asyncio
async def test_resolve_topic_answers_an_unscoped_topic_as_its_own_store_topic():
    app = Scoped()
    request = Request({"type": "http", "app": app.app, "headers": [], "path": "/", "method": "GET"})
    for topic in ("imports:7", "/nowhere"):
        resolved = await resolve_topic(request, topic)
        assert (resolved.topic, resolved.store_topic, resolved.scope) == (topic, topic, None)
    assert scopes_module._GATE_CACHE_SCOPE_KEY not in request.scope


@pytest.mark.asyncio
async def test_a_declared_source_is_not_affected_by_scopes():
    app = Scoped()
    imports = source("imports", authorizer=allow_all)
    settings.viewsets_event_authorizers = [ViewsetTopics(), DeclaredSources()]
    peer = FakePeer()
    assert (await app.subscribe("imports:7", peer)).payload == {"rev": 0}
    await imports.publish(TrackAdded(title="x"), key=7)
    await wait_for_pushes()
    assert peer.pushes[0][1][":topic"] == "imports:7"
    assert ":scope" not in peer.pushes[0][1]
