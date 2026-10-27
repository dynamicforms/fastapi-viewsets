"""A sub-viewset declaring its parent's event scope name: the declaration check, and one store topic
for the parent's and the child's topics."""

from typing import Any

import pytest

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..action_configuration import action_configuration
from ..collection_viewset import CollectionViewSet
from ..conf import settings
from ..decorators import route_viewset, ViewSetRouter
from ..middleware import Middleware
from ..mixins import ViewSetMixin
from ..mux_ws.registry import reset_registry
from .authorizers import ViewsetTopics
from .changes import ChangeEvents
from .conftest import FakePeer
from .delivery import subscriptions, wait_for_pushes
from .scopes import event_scope, reset_event_scopes, resolve_topic, scope_of
from .scopes_test import org_scope, pushed, SelectedOrg, user
from .viewset import register_events
from .viewset_test import FakeStream


def key(_request, _context) -> str:
    return "1"


@pytest.fixture(autouse=True)
def _reset():
    reset_event_scopes()
    reset_registry()
    saved = settings.viewsets_command_middleware, settings.viewsets_context_processors
    settings.viewsets_context_processors = []
    yield
    settings.viewsets_command_middleware, settings.viewsets_context_processors = saved
    reset_event_scopes()
    reset_registry()


# declaration


def test_a_child_decorated_before_its_parent_shares_the_name_once_the_parent_mounts_it():
    @event_scope(key, name="accounts")
    class Members:
        pass

    @event_scope(key, name="accounts")
    class Accounts:
        __router = ViewSetRouter()
        __router.sub_viewset("members", Members)

    assert scope_of(Members).name == scope_of(Accounts).name == "accounts"


def test_a_child_decorated_after_its_parent_shares_the_name():
    class Members:
        pass

    @event_scope(key, name="accounts")
    class Accounts:
        __router = ViewSetRouter()
        __router.sub_viewset("members", Members)

    event_scope(key, name="accounts")(Members)
    assert scope_of(Members).name == "accounts"


def test_classes_mounted_through_further_mounts_and_their_subclasses_share_the_name():
    class Active:
        pass

    class Members:
        __router = ViewSetRouter()
        __router.sub_viewset("active", Active)

    @event_scope(key, name="accounts")
    class Accounts:
        __router = ViewSetRouter()
        __router.sub_viewset("members", Members)

    event_scope(key, name="accounts")(Active)

    class NarrowerActive(Active):
        pass

    event_scope(key, name="accounts")(NarrowerActive)
    event_scope(key, name="accounts")(Members)
    assert {scope_of(cls).name for cls in (Accounts, Members, Active, NarrowerActive)} == {"accounts"}


def test_a_class_mounted_under_a_sharer_shares_the_name():
    class Active:
        pass

    @event_scope(key, name="accounts")
    class Members:
        __router = ViewSetRouter()
        __router.sub_viewset("active", Active)

    @event_scope(key, name="accounts")
    class Accounts:
        __router = ViewSetRouter()
        __router.sub_viewset("members", Members)

    event_scope(key, name="accounts")(Active)
    assert scope_of(Active).name == "accounts"


def test_an_unrelated_class_is_still_refused():
    class Members:
        pass

    @event_scope(key, name="accounts")
    class Accounts:
        __router = ViewSetRouter()
        __router.sub_viewset("members", Members)

    with pytest.raises(ValueError, match="nor mounted under or mounting"):

        @event_scope(key, name="accounts")
        class Stranger:
            pass

    # a class mounted under an unrelated class is no sharer either
    with pytest.raises(ValueError, match="accounts"):

        @event_scope(key, name="accounts")
        class Elsewhere:
            __router = ViewSetRouter()
            __router.sub_viewset("x", type("Unrelated", (), {}))


# one store topic


class Account(BaseModel):
    id: int = Field(json_schema_extra={"autoinc_int": True})
    name: str


class MemberRow(BaseModel):
    id: int
    name: str


class ParentGate(Middleware):
    """Refuses the parent's `members` action for the user `eve`."""

    async def depends(self, request, cls, context) -> None:
        if context.action_name == "members" and request.headers.get("x-user") == "eve":
            raise HTTPException(403, f"{cls.__name__} refuses eve")

    async def __call__(self, _request, _viewset, _context, call_next):
        return await call_next()


class App:
    def __init__(self):
        self.selected = SelectedOrg()
        settings.viewsets_command_middleware = [ParentGate(), self.selected, ChangeEvents()]
        settings.viewsets_event_authorizers = [ViewsetTopics()]
        accounts: dict[int, Account] = {}
        router, events = APIRouter(), APIRouter()

        @event_scope(org_scope, name="accounts")
        class MembersViewSet(CollectionViewSet[int, MemberRow], ViewSetMixin[int, MemberRow]):
            def __init__(self):
                super().__init__(container={}, pk_field="id")

            async def perform_list(self, _context) -> list[MemberRow]:
                return [MemberRow(id=a.id, name=a.name) for a in accounts.values()]

        @event_scope(org_scope, name="accounts")
        @route_viewset(router, "/accounts", pk_field_name="id")
        class AccountViewSet(CollectionViewSet[int, Account], ViewSetMixin[int, Account]):
            __router = ViewSetRouter()
            __router.sub_viewset("members", MembersViewSet, pk_field_name="id")

            def __init__(self):
                super().__init__(container=accounts, pk_field="id")

        self.events = action_configuration({SelectedOrg: False})(register_events(events))
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.include_router(events)

    async def command(self, path: str, payload: Any, peer: Any = None, method: str = "POST", **headers):
        from ..mux_ws.server import process_command

        stream = FakeStream({":method": method, ":path": path, **headers})
        assert await process_command(payload, stream, app=self.app, peer=peer) is True
        return stream


@pytest.mark.asyncio
async def test_the_parent_s_writes_reach_the_subscribers_of_the_child_s_topic():
    app = App()
    child, parent = FakePeer(), FakePeer()
    stream = await app.command("/events/subscribe", {"topic": "/accounts/members"}, child, **user("ana", "7"))
    assert (stream.status, stream.payload) == (200, {"rev": 0, "scope": "7"})
    stream = await app.command("/events/subscribe", {"topic": "/accounts"}, parent, **user("ana", "7"))
    assert (stream.status, stream.payload) == (200, {"rev": 0, "scope": "7"})
    assert {s.peer for s in subscriptions("@accounts:7")} == {child, parent}

    stream = await app.command("/accounts", {"name": "Ana"}, **user("ana", "7"))
    assert stream.status == 200
    await wait_for_pushes()
    assert pushed(child) == [("/accounts/members", "7", "created")]
    assert pushed(parent) == [("/accounts", "7", "created")]

    listed = await app.command("/accounts/members", None, method="GET", **user("ana", "7"))
    assert listed.payload == [{"id": 1, "name": "Ana"}]


@pytest.mark.asyncio
async def test_the_parent_s_gate_refuses_a_subscription_of_the_child_s_topic():
    app = App()
    peer = FakePeer()
    stream = await app.command("/events/subscribe", {"topic": "/accounts/members"}, peer, **user("eve", "7"))
    assert stream.status == 403
    assert stream.payload["detail"] == "AccountViewSet refuses eve"
    # the parent's own topic is not behind the `members` action
    stream = await app.command("/events/subscribe", {"topic": "/accounts"}, peer, **user("bor", "8"))
    assert stream.status == 200


@pytest.mark.asyncio
async def test_the_child_s_topic_resolves_to_the_parent_s_store_topic():
    from starlette.requests import Request

    app = App()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/events/subscribe",
        "headers": [(b"x-user", b"ana"), (b"x-org", b"7")],
        "app": app.app,
        "query_string": b"",
    }
    resolved = await resolve_topic(Request(scope), "/accounts/members")
    assert (resolved.store_topic, resolved.scope) == ("@accounts:7", "7")
