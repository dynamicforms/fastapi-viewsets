"""
Viewsets mounted under another viewset with `ViewSetRouter.sub_viewset`: routing order over REST and
muxws, the parent's and the child's checks, nesting, metadata, `/schema` and the OpenAPI grouping.
"""

from typing import Any

import pytest

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from muxws.frames import ABSENT
from pydantic import BaseModel, Field

from ..action_configuration import action_configuration
from ..collection_viewset import CollectionViewSet
from ..conf import settings
from ..context import Context
from ..middleware import Middleware
from ..mixins import ListMixin, ViewSetMixin
from ..mux_ws.registry import registered_viewsets, reset_registry, schema_for
from ..mux_ws.server import process_command
from ..mux_ws.server_test import FakeStream
from . import route_viewset, SubViewSet, ViewSetRouter
from .route_viewset import MIDDLEWARE_DEPENDS_ATTRIBUTE, SUB_VIEWSET_GATE_ATTRIBUTE
from .sub_viewsets import mounted_under, sub_viewsets_of


class Account(BaseModel):
    id: int = Field(json_schema_extra={"autoinc_int": True})
    name: str
    email: str


class MemberRow(BaseModel):
    id: int
    name: str


ACCOUNTS = {
    1: Account(id=1, name="Ana", email="ana@example.com"),
    2: Account(id=2, name="Bor", email="bor@example.com"),
}


class Gate(Middleware):
    """Records every `depends()` as (viewset class name, action, configuration) and refuses the
    classes the `x-refuse` header names."""

    def __init__(self):
        self.seen: list[tuple[str, str | None, Any]] = []

    async def depends(self, request, cls, context) -> None:
        self.seen.append((cls.__name__, context.action_name, self.config_from(context)))
        if cls.__name__ in request.headers.get("x-refuse", "").split(","):
            raise HTTPException(403, f"{cls.__name__} refuses")

    async def __call__(self, _request, _viewset, _context, call_next):
        return await call_next()


@pytest.fixture(autouse=True)
def _reset_settings():
    saved = settings.viewsets_command_middleware, settings.viewsets_context_processors
    reset_registry()
    yield
    settings.viewsets_command_middleware, settings.viewsets_context_processors = saved
    reset_registry()


class Members(CollectionViewSet[int, MemberRow], ViewSetMixin[int, MemberRow]):
    """The members of the accounts, one row each."""

    def __init__(self, prefix: str = ""):
        self.prefix = prefix
        self.contexts: list[Context] = []
        super().__init__(
            container={pk: MemberRow(id=pk, name=prefix + a.name) for pk, a in ACCOUNTS.items()},
            pk_field="id",
        )

    async def perform_list(self, context: Context) -> list[MemberRow]:
        self.contexts.append(context)
        return await super().perform_list(context)


def fresh(name: str = "Members") -> type:
    """A class of its own for one test: routing sets class attributes a subclass would inherit."""
    return type(name, (Members,), {})


def accounts_app(members: Any = None, prefix: str = "", **options: Any) -> tuple[FastAPI, type]:
    router = APIRouter()
    mounted = members if members is not None else fresh("MembersViewSet")

    @route_viewset(router, "/accounts", pk_field_name="id")
    class AccountViewSet(CollectionViewSet[int, Account], ViewSetMixin[int, Account]):
        """Accounts of the application."""

        __router = ViewSetRouter()
        __router.sub_viewset("members", mounted, pk_field_name="id", **options)

        def __init__(self):
            super().__init__(container=dict(ACCOUNTS), pk_field="id")

        @__router.get("count")
        async def count(self, context: Context) -> int:
            return len(await self.perform_list(context))

    app = FastAPI()
    app.include_router(router, prefix=prefix)
    return app, AccountViewSet


# declaration


def test_a_plain_api_router_mounts_nothing_and_a_view_set_router_is_an_api_router():
    assert issubclass(ViewSetRouter, APIRouter)
    assert sub_viewsets_of(CollectionViewSet) == {}
    _app, account = accounts_app()
    [mount] = sub_viewsets_of(account).values()
    assert isinstance(mount, SubViewSet)
    assert (mount.path, mount.instance, mount.lifecycle, mount.pk_field_name) == ("members", None, "singleton", "id")
    assert list(mounted_under(account)) == [mount.viewset]


@pytest.mark.parametrize(
    ("path", "kwargs", "error", "match"),
    [
        ("a/b", {}, ValueError, "one path segment"),
        ("{pk}", {}, ValueError, "one path segment"),
        ("", {}, ValueError, "one path segment"),
        ("schema", {}, ValueError, "reserved"),
        ("x", {"lifecycle": "forever"}, ValueError, "unknown lifecycle"),
    ],
)
def test_sub_viewset_refuses_a_bad_mount(path, kwargs, error, match):
    with pytest.raises(error, match=match):
        ViewSetRouter().sub_viewset(path, fresh(), **kwargs)


def test_sub_viewset_refuses_a_second_mount_of_a_path_and_a_lifecycle_for_an_instance():
    router = ViewSetRouter()
    assert router.sub_viewset("/members/", Members).path == "members"
    with pytest.raises(ValueError, match="already mounted"):
        router.sub_viewset("members", Members)
    with pytest.raises(ValueError, match="needs a class"):
        router.sub_viewset("other", Members(), lifecycle="per-request")
    with pytest.raises(TypeError, match="neither"):
        router.sub_viewset("third", 7)


def test_a_mount_on_a_path_the_parent_serves_itself_is_refused():
    with pytest.raises(ValueError, match="serves '/accounts/members' itself"):

        @route_viewset(APIRouter(), "/accounts")
        class AccountViewSet(ListMixin[Account]):
            __router = ViewSetRouter()
            __router.sub_viewset("members", Members)

            @__router.get("members")
            async def members(self) -> list[Account]:
                return []


def test_a_viewset_mounted_under_itself_is_refused():
    class Loop(ListMixin[Account]):
        __router = ViewSetRouter()

    Loop._Loop__router.sub_viewset("again", Loop)
    with pytest.raises(ValueError, match="a viewset it is mounted under"):
        route_viewset(APIRouter(), "/loop")(Loop)


# routing order


def test_the_mount_is_matched_before_the_parent_s_pk_route_over_rest():
    app, _account = accounts_app()
    client = TestClient(app)
    assert client.get("/accounts/members").json() == [{"id": 1, "name": "Ana"}, {"id": 2, "name": "Bor"}]
    assert client.get("/accounts/members/2").json() == {"id": 2, "name": "Bor"}
    assert client.get("/accounts/2").json() == ACCOUNTS[2].model_dump()
    assert client.get("/accounts/count").json() == 2
    assert client.get("/accounts").status_code == 200
    assert client.get("/accounts/nobody").status_code == 422


@pytest.mark.asyncio
async def test_the_mount_is_matched_before_the_parent_s_pk_route_over_muxws():
    app, _account = accounts_app(prefix="/api")
    for path, expected in [
        ("/api/accounts/members", [{"id": 1, "name": "Ana"}, {"id": 2, "name": "Bor"}]),
        ("/api/accounts/members/1", {"id": 1, "name": "Ana"}),
        ("/api/accounts/1", ACCOUNTS[1].model_dump()),
    ]:
        stream = FakeStream({":method": "GET", ":path": path})
        assert await process_command(ABSENT, stream, app=app) is True
        assert (stream.status, stream.payload) == (200, expected)


def test_an_instance_is_the_singleton():
    members = fresh()(prefix="m:")
    app, account = accounts_app(members=members)
    assert TestClient(app).get("/accounts/members/1").json() == {"id": 1, "name": "m:Ana"}
    assert len(members.contexts) == 0
    TestClient(app).get("/accounts/members")
    assert len(members.contexts) == 1
    assert sub_viewsets_of(account)["members"].instance is members


def test_a_per_request_child_gets_an_instance_per_request():
    created: list[Any] = []

    class Counted(Members):
        def __init__(self):
            created.append(self)
            super().__init__()

    app, _account = accounts_app(members=Counted, lifecycle="per-request")
    client = TestClient(app)
    client.get("/accounts/members")
    client.get("/accounts/members")
    assert len(created) == 2


# metadata, /schema, muxws registration


def test_the_child_carries_its_full_base_path_and_its_own_schema():
    app, account = accounts_app(prefix="/api")
    child = account.__viewset_metadata__["sub_viewsets"]["members"]
    assert account.__viewset_metadata__["parent"] is None
    assert child.__viewset_metadata__["base_path"] == "/accounts/members"
    assert child.__viewset_metadata__["parent"] is account
    assert child.__viewset_metadata__["router"] is account.__viewset_metadata__["router"]
    assert child.__viewset_metadata__["paths"]["list_items"] == ""
    assert registered_viewsets()[child] == "/accounts/members"

    schema = TestClient(app).get("/api/accounts/members/schema").json()
    assert set(schema["paths"]) == {"/accounts/members", "/accounts/members/{pk}", "/accounts/members/schema"}
    assert set(schema_for("/accounts/members")["paths"]) == set(schema["paths"])
    parent_schema = TestClient(app).get("/api/accounts/schema").json()
    assert not any(path.startswith("/accounts/members") for path in parent_schema["paths"])


def test_the_mount_takes_the_parent_s_transports_unless_it_states_its_own():
    router = APIRouter()

    @route_viewset(router, "/accounts", register_muxws=False)
    class AccountViewSet(ListMixin[Account]):
        __router = ViewSetRouter()
        __router.sub_viewset("members", fresh("MembersViewSet"))
        __router.sub_viewset("rows", fresh("RowsViewSet"), register_muxws=True)

        async def perform_list(self, _context) -> list[Account]:
            return []

    assert set(registered_viewsets().values()) == {"/accounts/rows"}


# gates


def gated_app(**options: Any) -> tuple[TestClient, Gate, type]:
    gate = Gate()
    settings.viewsets_command_middleware = [gate]
    app, account = accounts_app(**options)
    return TestClient(app), gate, account


def test_both_gates_admit_and_the_parent_s_runs_first():
    client, gate, _account = gated_app()
    assert client.get("/accounts/members").status_code == 200
    assert gate.seen == [("AccountViewSet", "members", None), ("MembersViewSet", "list_items", None)]


def test_a_refusing_parent_gate_keeps_the_child_from_running():
    client, gate, account = gated_app()
    response = client.get("/accounts/members", headers={"x-refuse": "AccountViewSet"})
    assert response.status_code == 403
    assert response.json()["detail"] == "AccountViewSet refuses"
    assert gate.seen == [("AccountViewSet", "members", None)]
    assert account.__viewset_metadata__["sub_viewsets"]["members"]
    # the parent's own endpoints are refused by the same header, with its own action
    assert client.get("/accounts/1", headers={"x-refuse": "AccountViewSet"}).status_code == 403


def test_a_refusing_child_gate_refuses_after_the_parent_admitted():
    members = fresh()()
    client, gate, _account = gated_app(members=members)
    response = client.get("/accounts/members", headers={"x-refuse": "Members"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Members refuses"
    assert [entry[0] for entry in gate.seen] == ["AccountViewSet", "Members"]
    assert members.contexts == []


def test_the_parent_s_own_routes_do_not_run_the_mount_gate():
    client, gate, _account = gated_app()
    client.get("/accounts")
    assert gate.seen == [("AccountViewSet", "list_items", None)]


def test_the_mount_configuration_overrides_the_parent_s_class_configuration():
    gate = Gate()
    settings.viewsets_command_middleware = [gate]
    router = APIRouter()

    @route_viewset(router, "/accounts")
    @action_configuration({Gate: "class"})
    class AccountViewSet(ListMixin[Account]):
        __router = ViewSetRouter()
        __router.sub_viewset("members", fresh("MembersViewSet"), action_configuration={Gate: "mount"})
        __router.sub_viewset("rows", fresh("RowsViewSet"))

        async def perform_list(self, _context) -> list[Account]:
            return []

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    client.get("/accounts/members")
    client.get("/accounts/rows")
    client.get("/accounts")
    assert gate.seen == [
        ("AccountViewSet", "members", "mount"),
        ("MembersViewSet", "list_items", None),
        ("AccountViewSet", "rows", "class"),
        ("RowsViewSet", "list_items", None),
        ("AccountViewSet", "list_items", "class"),
    ]


def test_each_class_gets_a_context_of_its_own():
    seen: list[tuple[str, str]] = []

    async def who(_request, viewset) -> dict[str, Any]:
        name = viewset.__name__ if isinstance(viewset, type) else type(viewset).__name__
        seen.append(name)
        return {"who": name}

    settings.viewsets_context_processors = [who]
    members = fresh()()
    app, _account = accounts_app(members=members)
    assert TestClient(app).get("/accounts/members").status_code == 200
    assert seen == ["AccountViewSet", "Members"]
    [context] = members.contexts
    assert (context.raw()["who"], context.action_name) == ("Members", "list_items")


def test_a_refusing_parent_context_processor_refuses_the_child():
    async def refuse_parent(_request, viewset) -> dict[str, Any]:
        if viewset.__name__ == "AccountViewSet":
            raise HTTPException(401, "who are you")
        return {}

    settings.viewsets_context_processors = [refuse_parent]
    members = fresh()()
    app, _account = accounts_app(members=members)
    assert TestClient(app).get("/accounts/members").status_code == 401
    assert members.contexts == []


def test_the_security_scheme_guards_the_child():
    from fastapi.security import HTTPBearer

    saved = settings.viewsets_security_scheme
    settings.viewsets_security_scheme = HTTPBearer()
    try:
        app, _account = accounts_app()
        client = TestClient(app)
        assert client.get("/accounts/members").status_code in (401, 403)
        assert client.get("/accounts/members", headers={"Authorization": "Bearer t"}).status_code == 200
    finally:
        settings.viewsets_security_scheme = saved


def test_a_route_carries_the_gates_outermost_first_and_its_own_last():
    router = APIRouter()

    class ActiveViewSet(Members):
        pass

    class MembersViewSet(Members):
        __router = ViewSetRouter()
        __router.sub_viewset("active", ActiveViewSet)

    @route_viewset(router, "/accounts")
    class AccountViewSet(ListMixin[Account]):
        __router = ViewSetRouter()
        __router.sub_viewset("members", MembersViewSet)

        async def perform_list(self, _context) -> list[Account]:
            return []

    [route] = [r for r in router.routes if r.path == "/accounts/members/active" and "GET" in r.methods]
    *gates, own = [depends.dependency for depends in route.dependencies]
    assert [getattr(gate, SUB_VIEWSET_GATE_ATTRIBUTE) for gate in gates] == [
        (AccountViewSet, "members"),
        (MembersViewSet, "active"),
    ]
    assert getattr(own, MIDDLEWARE_DEPENDS_ATTRIBUTE) is True
    paths = [r.path for r in router.routes]
    assert paths.index("/accounts/members/active") < paths.index("/accounts/members/{pk}")
    assert paths.index("/accounts/members/{pk}") < paths.index("/accounts")


def test_nesting_is_recursive_and_every_level_s_gate_runs():
    gate = Gate()
    settings.viewsets_command_middleware = [gate]
    router = APIRouter()

    class ActiveViewSet(Members):
        pass

    class MembersViewSet(Members):
        __router = ViewSetRouter()
        __router.sub_viewset("active", ActiveViewSet)

    @route_viewset(router, "/accounts")
    class AccountViewSet(CollectionViewSet[int, Account], ViewSetMixin[int, Account]):
        __router = ViewSetRouter()
        __router.sub_viewset("members", MembersViewSet)

        def __init__(self):
            super().__init__(container=dict(ACCOUNTS), pk_field="id")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/accounts/members/active").json() == [{"id": 1, "name": "Ana"}, {"id": 2, "name": "Bor"}]
    assert gate.seen == [
        ("AccountViewSet", "members", None),
        ("MembersViewSet", "active", None),
        ("ActiveViewSet", "list_items", None),
    ]
    assert client.get("/accounts/members/active", headers={"x-refuse": "MembersViewSet"}).status_code == 403
    assert ActiveViewSet.__viewset_metadata__["base_path"] == "/accounts/members/active"
    assert ActiveViewSet.__viewset_metadata__["parent"] is MembersViewSet
    assert list(mounted_under(AccountViewSet)) == [MembersViewSet, ActiveViewSet]


def test_a_mount_of_a_base_class_is_inherited_and_a_subclass_replaces_it():
    class Base(ListMixin[Account]):
        __router = ViewSetRouter()
        __router.sub_viewset("members", Members)

        async def perform_list(self, _context) -> list[Account]:
            return []

    class Other(Members):
        pass

    class Derived(Base):
        __router = ViewSetRouter()
        __router.sub_viewset("members", Other)

    assert sub_viewsets_of(Base)["members"].viewset is Members
    assert sub_viewsets_of(Derived)["members"].viewset is Other


# OpenAPI


def test_the_child_s_operations_are_listed_in_the_parent_s_group():
    app, _account = accounts_app()
    client = TestClient(app)
    document = client.get("/openapi.json").json()
    operations = [
        (path, method, operation)
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if path.startswith("/accounts/members")
    ]
    assert {path for path, _method, _operation in operations} == {
        "/accounts/members",
        "/accounts/members/{pk}",
        "/accounts/members/schema",
    }
    assert {tag for _path, _method, operation in operations for tag in operation["tags"]} == {"Account"}
    summaries = {(path, method): operation["summary"] for path, method, operation in operations}
    assert summaries[("/accounts/members", "get")] == "Members: List Items"
    assert summaries[("/accounts/members/{pk}", "delete")] == "Members: Destroy"
    assert document["paths"]["/accounts"]["get"]["summary"] == "List Items"
    every_id = [operation["operationId"] for item in document["paths"].values() for operation in item.values()]
    assert len(every_id) == len(set(every_id))
    every_summary = [
        (operation["summary"], operation["tags"][0])
        for item in document["paths"].values()
        for operation in item.values()
    ]
    assert len(every_summary) == len(set(every_summary))

    # the child's docstring does not open a group of its own
    from ..endpoint_docs import apply_viewset_tags, viewset_tags

    names = {tag["name"] for tag in viewset_tags()}
    assert "Members" not in names
    assert "MembersViewSet" not in names
    apply_viewset_tags(app)
    tags = {tag["name"]: tag.get("description") for tag in client.get("/openapi.json").json()["tags"]}
    assert tags["Account"] == "Accounts of the application."
    assert "Members" not in tags

    for page in ("/docs", "/redoc"):
        response = client.get(page)
        assert response.status_code == 200
        assert "/openapi.json" in response.text


def test_an_explicit_summary_of_the_child_is_kept():
    from ..endpoint_docs import endpoint_docs

    @endpoint_docs({"list_items": {"summary": "Browse the members"}})
    class MembersViewSet(Members):
        pass

    app, _account = accounts_app(members=MembersViewSet)
    document = TestClient(app).get("/openapi.json").json()
    assert document["paths"]["/accounts/members"]["get"]["summary"] == "Browse the members"


def test_one_class_mounted_twice_serves_each_mount_with_its_own_instance_and_path():
    cls = fresh()
    router = APIRouter()

    @route_viewset(router, "/accounts")
    class AccountViewSet(ListMixin[Account]):
        __router = ViewSetRouter()
        __router.sub_viewset("members", cls(prefix="a:"))

        async def perform_list(self, _context) -> list[Account]:
            return []

    @route_viewset(router, "/orgs")
    class OrgViewSet(ListMixin[Account]):
        __router = ViewSetRouter()
        __router.sub_viewset("members", cls(prefix="o:"))

        async def perform_list(self, _context) -> list[Account]:
            return []

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/accounts/members/1").json() == {"id": 1, "name": "a:Ana"}
    assert client.get("/orgs/members/1").json() == {"id": 1, "name": "o:Ana"}
    assert set(client.get("/accounts/members/schema").json()["paths"]) >= {"/accounts/members"}
    assert set(client.get("/orgs/members/schema").json()["paths"]) >= {"/orgs/members"}


# gates over muxws


async def muxws_get(app: FastAPI, path: str, **headers: str) -> FakeStream:
    stream = FakeStream({":method": "GET", ":path": path, **headers})
    assert await process_command(ABSENT, stream, app=app) is True
    return stream


@pytest.mark.asyncio
async def test_both_gates_admit_and_the_parent_s_runs_first_over_muxws():
    gate = Gate()
    settings.viewsets_command_middleware = [gate]
    members = fresh()()
    app, _account = accounts_app(members=members)
    stream = await muxws_get(app, "/accounts/members")
    assert (stream.status, stream.payload) == (200, [{"id": 1, "name": "Ana"}, {"id": 2, "name": "Bor"}])
    assert gate.seen == [("AccountViewSet", "members", None), ("Members", "list_items", None)]
    assert len(members.contexts) == 1


@pytest.mark.asyncio
async def test_a_refusing_parent_gate_keeps_the_child_from_running_over_muxws():
    gate = Gate()
    settings.viewsets_command_middleware = [gate]
    members = fresh()()
    app, _account = accounts_app(members=members)
    stream = await muxws_get(app, "/accounts/members", **{"x-refuse": "AccountViewSet"})
    assert stream.status == 403
    assert stream.payload["detail"] == "AccountViewSet refuses"
    assert gate.seen == [("AccountViewSet", "members", None)]
    assert members.contexts == []


@pytest.mark.asyncio
async def test_a_refusing_child_gate_refuses_after_the_parent_admitted_over_muxws():
    gate = Gate()
    settings.viewsets_command_middleware = [gate]
    members = fresh()()
    app, _account = accounts_app(members=members)
    stream = await muxws_get(app, "/accounts/members", **{"x-refuse": "Members"})
    assert stream.status == 403
    assert stream.payload["detail"] == "Members refuses"
    assert gate.seen == [("AccountViewSet", "members", None), ("Members", "list_items", None)]
    assert members.contexts == []
