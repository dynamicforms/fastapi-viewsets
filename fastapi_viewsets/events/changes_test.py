import asyncio

from typing import Any
from unittest.mock import MagicMock

import pytest

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from muxws.frames import ABSENT
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.routing import Mount

from ..action_configuration import action_configuration
from ..collection_viewset import CollectionViewSet
from ..conf import settings
from ..context import Context
from ..decorators import route_viewset
from ..decorators.celery_viewset import celery_viewset_client, celery_viewset_server
from ..decorators.lifecycle_runner import lifecycle_runner
from ..decorators.lifecycle_runner_test import _Executor
from ..exceptions import df_viewset_exception_handler, DfViewSetError
from ..middleware import ActionExecution, ExecutionRef, ViewSetResult
from ..mixins import BulkViewSetMixin, CreateMixin, DestroyMixin, ViewSetMixin
from ..mux_ws.registry import reset_registry
from ..mux_ws.server import process_command
from ..mux_ws.server_test import FakeStream
from .authorizers import find_viewset
from .changes import Change, ChangeEvents, RecordsChanged, routed_base_path, TOPIC_CONTEXT_KEY
from .event import Event
from .sources import publish
from .store import get_store


class Track(BaseModel):
    id: int = Field(default=0, json_schema_extra={"autoinc_int": True})
    title: str


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry()
    yield
    reset_registry()


@pytest.fixture(autouse=True)
def _chain(monkeypatch):
    """The global command middleware: one ChangeEvents."""
    monkeypatch.setattr(settings, "viewsets_command_middleware", [ChangeEvents()])


def logged(topic: str) -> list[tuple[str, Any]]:
    """The events of `topic` in the store, oldest first, as (name, payload)."""
    entries = asyncio.run(get_store().since(topic, 0))
    return [(entry.name, entry.payload) for entry in entries]


def changes(*pks) -> list[dict]:
    return [{"pk": pk} for pk in pks]


def app_for(router: APIRouter, prefix: str = "") -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(DfViewSetError, df_viewset_exception_handler)
    app.include_router(router, prefix=prefix)
    return app


def collection_viewset(
    router: APIRouter, container: list[Track] | None = None, decorators=(), base_path: str = "/tracks", **route_kwargs
) -> type:
    container = [Track(id=1, title="One"), Track(id=2, title="Two")] if container is None else container

    class TrackViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=container, pk_field="id")

    viewset_class = TrackViewSet
    for decorator in decorators:
        viewset_class = decorator(viewset_class)
    return route_viewset(router, base_path, **{"pk_field_name": "id", **route_kwargs})(viewset_class)


# every action, over CollectionViewSet and DjangoORMViewSet

WRITES = [
    pytest.param("POST", "/tracks", {"title": "Three"}, "created", [3], id="create"),
    pytest.param("POST", "/tracks/bulk", [{"title": "Three"}, {"title": "Four"}], "created", [3, 4], id="bulk_create"),
    pytest.param("PUT", "/tracks/1", {"id": 1, "title": "Uno"}, "updated", [1], id="update"),
    pytest.param("PATCH", "/tracks/2", {"title": "Dos"}, "updated", [2], id="partial_update"),
    pytest.param(
        "PUT",
        "/tracks/bulk",
        {"1": {"id": 1, "title": "Uno"}, "2": {"id": 2, "title": "Dos"}},
        "updated",
        [1, 2],
        id="bulk_update",
    ),
    pytest.param("PATCH", "/tracks/bulk", {"2": {"title": "Dos"}}, "updated", [2], id="bulk_partial_update"),
    pytest.param("DELETE", "/tracks/2", None, "deleted", [2], id="destroy"),
    pytest.param("DELETE", "/tracks/bulk", [1, 2], "deleted", [1, 2], id="bulk_destroy"),
]

READS = [
    pytest.param("GET", "/tracks", id="list_items"),
    pytest.param("GET", "/tracks/1", id="retrieve"),
    pytest.param("GET", "/tracks/schema", id="schema"),
]


@pytest.mark.parametrize(("method", "path", "body", "op", "pks"), WRITES)
def test_each_write_action_of_a_collection_viewset_publishes_one_event(method, path, body, op, pks):
    router = APIRouter()
    collection_viewset(router)
    response = TestClient(app_for(router)).request(method, path, json=body)
    assert response.status_code == 200, response.text
    assert logged("/tracks") == [("records_changed", {"op": op, "changes": changes(*pks)})]


@pytest.mark.parametrize(("method", "path"), READS)
def test_a_read_action_publishes_nothing(method, path):
    router = APIRouter()
    collection_viewset(router)
    assert TestClient(app_for(router)).request(method, path).status_code == 200
    assert logged("/tracks") == []


def test_each_event_links_to_the_one_before_it():
    router = APIRouter()
    collection_viewset(router)
    client = TestClient(app_for(router))
    client.post("/tracks", json={"title": "Three"})
    client.delete("/tracks/3")
    first, second = asyncio.run(get_store().since("/tracks", 0))
    assert first.prev == 0
    assert second.prev == first.rev
    assert second.rev > first.rev


@pytest.fixture(scope="module")
def django_table():
    pytest.importorskip("django")
    from django.db import connection, models

    class ChangeTrackModel(models.Model):
        title = models.CharField(max_length=200)

        class Meta:
            app_label = "backends"

    with connection.schema_editor() as editor:
        editor.create_model(ChangeTrackModel)
    yield ChangeTrackModel
    with connection.schema_editor() as editor:
        editor.delete_model(ChangeTrackModel)


@pytest.fixture
def django_viewset(django_table):
    from ..backends.django_orm import DjangoORMViewSet

    django_table.objects.all().delete()
    django_table.objects.create(id=1, title="One")
    django_table.objects.create(id=2, title="Two")
    router = APIRouter()

    # no pk_field_name argument: the pk field is the backend's own pk_field_name
    @route_viewset(router, "/tracks")
    class TrackViewSet(DjangoORMViewSet[int, Track], BulkViewSetMixin[int, Track]):
        model = django_table
        schema = Track

        async def perform_destroy(self, context, pk):
            # the answer in the shape DestroyMixin declares, dict[K, Any]
            await super().perform_destroy(context, pk)
            return {pk: None}

    return TestClient(app_for(router))


@pytest.mark.parametrize(("method", "path", "body", "op", "pks"), WRITES)
def test_each_write_action_of_a_django_viewset_publishes_one_event(django_viewset, method, path, body, op, pks):
    response = django_viewset.request(method, path, json=body)
    assert response.status_code == 200, response.text
    if op == "created":
        # the database assigns the keys of new rows
        pks = [record["id"] for record in (response.json() if isinstance(body, list) else [response.json()])]
    assert logged("/tracks") == [("records_changed", {"op": op, "changes": changes(*pks)})]


@pytest.mark.parametrize(("method", "path"), READS)
def test_a_read_action_of_a_django_viewset_publishes_nothing(django_viewset, method, path):
    assert django_viewset.request(method, path).status_code == 200
    assert logged("/tracks") == []


# configuration


def test_false_on_the_class_disables_every_action():
    router = APIRouter()
    collection_viewset(router, decorators=[action_configuration({ChangeEvents: False})])
    client = TestClient(app_for(router))
    assert client.post("/tracks", json={"title": "Three"}).status_code == 200
    assert client.delete("/tracks/1").status_code == 200
    assert logged("/tracks") == []


def test_false_on_a_perform_method_disables_its_action_only():
    container = [Track(id=1, title="One"), Track(id=2, title="Two")]
    router = APIRouter()

    @route_viewset(router, "/tracks", pk_field_name="id")
    class TrackViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=container, pk_field="id")

        @action_configuration({ChangeEvents: False})
        async def perform_destroy(self, context, pk):
            return await super().perform_destroy(context, pk)

    client = TestClient(app_for(router))
    assert client.delete("/tracks/1").status_code == 200
    assert logged("/tracks") == []
    assert client.request("DELETE", "/tracks/bulk", json=[2]).status_code == 200
    assert logged("/tracks") == [("records_changed", {"op": "deleted", "changes": changes(2)})]


def test_a_configured_topic_replaces_the_routed_base_path():
    router = APIRouter()
    collection_viewset(router, decorators=[action_configuration({ChangeEvents: {"topic": "catalog:7"}})])
    TestClient(app_for(router, prefix="/api")).delete("/api/tracks/1")
    assert logged("/api/tracks") == []
    assert logged("catalog:7") == [("records_changed", {"op": "deleted", "changes": changes(1)})]


def test_a_configured_topic_may_be_a_callable_of_the_execution():
    seen: list[ActionExecution] = []

    def topic(execution: ActionExecution) -> str:
        seen.append(execution)
        return f"/custom/{execution.action}"

    router = APIRouter()
    collection_viewset(router, decorators=[action_configuration({ChangeEvents: {"topic": topic}})])
    TestClient(app_for(router)).delete("/tracks/1")
    assert logged("/custom/destroy") == [("records_changed", {"op": "deleted", "changes": changes(1)})]
    assert seen[0].kwargs["pk"] == 1


def test_a_configured_topic_of_neither_form_raises_before_the_action_runs():
    container = [Track(id=1, title="One")]
    router = APIRouter()
    collection_viewset(router, container, decorators=[action_configuration({ChangeEvents: {"topic": "Not A Topic"}})])
    client = TestClient(app_for(router), raise_server_exceptions=False)
    assert client.delete("/tracks/1").status_code == 500
    assert container == [Track(id=1, title="One")]


@pytest.mark.parametrize("config", ["yes", {"records": True}])
def test_a_value_of_another_shape_raises_type_error(config):
    events = ChangeEvents()
    with pytest.raises(TypeError, match="ChangeEvents is configured with"):
        events.options_for(Context({}, action_configuration={ChangeEvents: config}))


def test_a_record_option_raises_type_error():
    events = ChangeEvents()
    with pytest.raises(TypeError, match="unknown option\\(s\\) 'record'"):
        events.options_for(Context({}, action_configuration={ChangeEvents: {"record": True}}))


def test_true_and_no_value_are_the_defaults():
    events = ChangeEvents()
    assert events.options_for(Context({}, action_configuration={ChangeEvents: True})) == {}
    assert events.options_for(Context({})) == {}


# the topic


def test_a_router_included_with_a_prefix_publishes_to_the_prefixed_path():
    inner = APIRouter()
    collection_viewset(inner)
    outer = APIRouter()
    outer.include_router(inner, prefix="/api")
    client = TestClient(app_for(outer, prefix="/v1"))
    assert client.delete("/v1/api/tracks/1").status_code == 200
    assert client.request("DELETE", "/v1/api/tracks/bulk", json=[2]).status_code == 200
    assert client.post("/v1/api/tracks", json={"title": "Three"}).status_code == 200
    assert [payload["op"] for _, payload in logged("/v1/api/tracks")] == ["deleted", "deleted", "created"]
    assert logged("/tracks") == []


def test_a_base_path_with_a_path_parameter_publishes_to_the_path_with_its_value():
    router = APIRouter()
    collection_viewset(router, base_path="/clubs/{club_id}/tracks")
    app = app_for(router)
    client = TestClient(app)
    assert client.delete("/clubs/9/tracks/1").status_code == 200
    assert client.post("/clubs/9/tracks", json={"title": "Three"}).status_code == 200
    assert [payload["op"] for _, payload in logged("/clubs/9/tracks")] == ["deleted", "created"]
    assert find_viewset(app, "/clubs/9/tracks") is not None


def test_a_prefix_with_a_path_parameter_publishes_to_the_path_with_its_value():
    router = APIRouter()
    viewset_class = collection_viewset(router)
    app = app_for(router, prefix="/clubs/{club_id}")
    assert TestClient(app).delete("/clubs/7/tracks/1").status_code == 200
    assert logged("/clubs/7/tracks") == [("records_changed", {"op": "deleted", "changes": changes(1)})]
    assert find_viewset(app, "/clubs/7/tracks") == (viewset_class, "list_items")


def test_one_viewset_mounted_twice_has_two_topics():
    router = APIRouter()
    collection_viewset(router)
    app = FastAPI()
    app.include_router(router, prefix="/a")
    app.include_router(router, prefix="/b")
    client = TestClient(app)
    client.delete("/a/tracks/1")
    client.delete("/b/tracks/2")
    assert logged("/a/tracks") == [("records_changed", {"op": "deleted", "changes": changes(1)})]
    assert logged("/b/tracks") == [("records_changed", {"op": "deleted", "changes": changes(2)})]


def test_the_mount_path_of_the_application_is_not_part_of_the_topic():
    router = APIRouter()
    collection_viewset(router)
    client = TestClient(Starlette(routes=[Mount("/sub", app=app_for(router, prefix="/api"))]))
    assert client.delete("/sub/api/tracks/1").status_code == 200
    assert [payload["op"] for _, payload in logged("/api/tracks")] == ["deleted"]


@pytest.mark.asyncio
async def test_a_muxws_command_publishes_to_the_same_topic():
    router = APIRouter()
    collection_viewset(router, register_muxws=True)
    stream = FakeStream({":method": "DELETE", ":path": "/api/tracks/1"})
    await process_command(ABSENT, stream, app=app_for(router, prefix="/api"))
    assert stream.status == 200
    entries = await get_store().since("/api/tracks", 0)
    assert [(entry.name, entry.payload) for entry in entries] == [
        ("records_changed", {"op": "deleted", "changes": changes(1)})
    ]


def test_every_action_finds_the_routed_base_path_in_the_context():
    """A custom endpoint publishes itself, to the topic `__call__` placed in the context."""
    router = APIRouter()

    @route_viewset(router, "/tracks", pk_field_name="id")
    class ArchiveViewSet:
        __router = APIRouter()

        @__router.post("/{pk}/archive")
        async def archive(self, context: Context, pk: int) -> None:
            await publish(await context[TOPIC_CONTEXT_KEY], RecordsChanged(op="deleted", changes=[Change(pk=pk)]))

    assert TestClient(app_for(router, prefix="/api")).post("/api/tracks/5/archive").status_code == 200
    assert logged("/api/tracks") == [("records_changed", {"op": "deleted", "changes": changes(5)})]


def test_routed_base_path_is_none_for_a_route_route_viewset_did_not_register():
    app = FastAPI()
    seen = []

    @app.get("/plain/{pk}")
    async def plain(request: Request) -> None:
        seen.append(routed_base_path(request))

    TestClient(app).get("/plain/1")
    assert seen == [None]


def test_an_execution_outside_any_call_publishes_to_the_base_path():
    router = APIRouter()
    viewset_class = collection_viewset(router)
    viewset = viewset_class()
    result = asyncio.run(
        lifecycle_runner(CreateMixin.create, viewset, viewset_class, needs_context=True, data=Track(title="Three"))
    )
    assert result == Track(id=3, title="Three")
    assert logged("/tracks") == [("records_changed", {"op": "created", "changes": changes(3)})]


# an injected instance


def test_an_injected_instance_publishes_for_its_viewset_alone(monkeypatch):
    monkeypatch.setattr(settings, "viewsets_command_middleware", [])
    router = APIRouter()
    collection_viewset(router, decorators=[action_configuration({ChangeEvents(): True})])
    other = APIRouter()

    @route_viewset(other, "/others", pk_field_name="id")
    class OtherViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=[Track(id=1, title="One")], pk_field="id")

    app = app_for(router)
    app.include_router(other)
    client = TestClient(app)
    client.patch("/tracks/1", json={"title": "Uno"})
    client.patch("/others/1", json={"title": "Uno"})
    assert logged("/tracks") == [("records_changed", {"op": "updated", "changes": changes(1)})]
    assert logged("/others") == []


def test_an_injected_instance_replaces_the_global_one_for_its_viewset():
    router = APIRouter()
    collection_viewset(router, decorators=[action_configuration({ChangeEvents(): {"topic": "/tracks:injected"}})])
    other = APIRouter()

    @route_viewset(other, "/others", pk_field_name="id")
    class OtherViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=[Track(id=1, title="One")], pk_field="id")

    app = app_for(router)
    app.include_router(other)
    client = TestClient(app)
    client.patch("/tracks/1", json={"title": "Uno"})
    client.patch("/others/1", json={"title": "Uno"})
    assert logged("/tracks:injected") == [("records_changed", {"op": "updated", "changes": changes(1)})]
    assert logged("/tracks") == []
    assert logged("/others") == [("records_changed", {"op": "updated", "changes": changes(1)})]


def test_an_instance_injected_on_a_perform_method_replaces_the_global_one_for_its_action():
    container = [Track(id=1, title="One"), Track(id=2, title="Two")]
    router = APIRouter()

    @route_viewset(router, "/tracks", pk_field_name="id")
    class TrackViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=container, pk_field="id")

        @action_configuration({ChangeEvents(): {"topic": "codes"}})
        async def perform_destroy(self, context, pk):
            return await super().perform_destroy(context, pk)

    client = TestClient(app_for(router))
    client.delete("/tracks/1")
    client.patch("/tracks/2", json={"title": "Dos"})
    assert logged("codes") == [("records_changed", {"op": "deleted", "changes": changes(1)})]
    assert logged("/tracks") == [("records_changed", {"op": "updated", "changes": changes(2)})]


def test_a_class_key_without_a_global_instance_raises_value_error_at_request_time(monkeypatch):
    monkeypatch.setattr(settings, "viewsets_command_middleware", [])
    container = [Track(id=1, title="One")]
    router = APIRouter()
    collection_viewset(router, container, decorators=[action_configuration({ChangeEvents: {"topic": "codes"}})])
    client = TestClient(app_for(router), raise_server_exceptions=True)
    with pytest.raises(ValueError, match="ChangeEvents is configured via @action_configuration"):
        client.delete("/tracks/1")
    assert container == [Track(id=1, title="One")]


# an action that raises


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param("PUT", "/tracks/9", {"id": 9, "title": "Nine"}, id="update"),
        pytest.param("DELETE", "/tracks/9", None, id="destroy"),
        pytest.param("DELETE", "/tracks/bulk", [1, 9], id="bulk_destroy"),
    ],
)
def test_an_action_that_raises_publishes_nothing(method, path, body):
    router = APIRouter()
    collection_viewset(router)
    assert TestClient(app_for(router)).request(method, path, json=body).status_code == 404
    assert logged("/tracks") == []


def test_an_exception_of_the_action_propagates_through_execute():
    router = APIRouter()

    @route_viewset(router, "/tracks", pk_field_name="id")
    class FailingViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=[], pk_field="id")

        async def perform_create(self, _context, _data):
            raise RuntimeError("disk full")

    client = TestClient(app_for(router), raise_server_exceptions=False)
    assert client.post("/tracks", json={"title": "Three"}).status_code == 500
    assert logged("/tracks") == []


def test_a_failing_append_after_the_action_reaches_the_caller(monkeypatch):
    async def unreachable(_topic, _name, _payload):
        raise ConnectionError("redis is unreachable")

    monkeypatch.setattr(get_store(), "append", unreachable)
    container = [Track(id=1, title="One")]
    router = APIRouter()
    collection_viewset(router, container)
    client = TestClient(app_for(router), raise_server_exceptions=False)
    assert client.post("/tracks", json={"title": "Two"}).status_code == 500
    assert container == [Track(id=1, title="One"), Track(id=2, title="Two")]


# the pk field


class Coded(BaseModel):
    code: str
    title: str


@pytest.mark.parametrize("with_list", [True, False], ids=["with-list", "without-list"])
def test_the_pk_field_of_a_collection_viewset_is_its_pk_field(with_list):
    container = [Coded(code="a", title="A")]
    router = APIRouter()

    class WithList(CollectionViewSet[str, Coded], ViewSetMixin[str, Coded]):
        def __init__(self):
            super().__init__(container=container, pk_field="code")

    class WithoutList(CollectionViewSet[str, Coded], CreateMixin[str, Coded], DestroyMixin[str, Coded]):
        def __init__(self):
            super().__init__(container=container, pk_field="code")

    viewset_class = WithList if with_list else WithoutList
    route_viewset(router, "/codes")(viewset_class)
    client = TestClient(app_for(router))
    assert client.post("/codes", json={"code": "b", "title": "B"}).status_code == 200
    assert client.delete("/codes/a").status_code == 200
    assert logged("/codes") == [
        ("records_changed", {"op": "created", "changes": changes("b")}),
        ("records_changed", {"op": "deleted", "changes": changes("a")}),
    ]


def test_the_pk_field_falls_back_to_the_pk_field_attribute():
    class Plain:
        pk_field = "code"

    execution = ActionExecution(Plain, Plain(), "create", (), {}, Context({}), None, None)
    assert ChangeEvents().pk_field_for(execution) == "code"
    assert ChangeEvents().changes_for(execution, "created", {"code": "b"}) == [Change(pk="b")]


def test_the_pk_field_name_attribute_comes_before_the_pk_field_attribute():
    class Backend:
        pk_field = "pk"
        pk_field_name = "id"

    execution = ActionExecution(Backend, Backend(), "update", (), {}, Context({}), None, None)
    assert ChangeEvents().pk_field_for(execution) == "id"


def test_a_viewset_that_names_no_pk_field_raises_type_error_before_the_action_runs():
    created = []
    router = APIRouter()

    @route_viewset(router, "/anonymous")
    class AnonymousViewSet(CreateMixin[int, Track]):
        async def perform_create(self, _context, data):
            created.append(data)
            return data

    client = TestClient(app_for(router), raise_server_exceptions=True)
    with pytest.raises(TypeError, match="AnonymousViewSet names no pk field"):
        client.post("/anonymous", json={"title": "Three"})
    assert created == []


def test_a_result_returned_as_a_viewset_result_contributes_its_body():
    class Plain:
        pk_field_name = "id"

    execution = ActionExecution(Plain, Plain(), "create", (), {}, Context({}), None, None)
    result = ViewSetResult(body=Track(id=4, title="Four"), status_code=201)
    assert ChangeEvents().changes_for(execution, "created", result) == [Change(pk=4)]


def test_a_pk_passed_by_position_is_read_from_the_arguments():
    class Plain:
        async def bulk_destroy(self, _context, pk):
            return pk

    execution = ActionExecution(Plain, Plain(), "bulk_destroy", (None, [1, 2]), {}, Context({}), None, None)
    assert ChangeEvents().changes_for(execution, "deleted", None) == [Change(pk=1), Change(pk=2)]


# the hooks


def test_a_subclass_replaces_the_topic_and_the_changes(monkeypatch):
    class Narrow(ChangeEvents):
        def topic_for(self, execution):
            return f"{super().topic_for(execution)}:all"

        def changes_for(self, execution, op, result):
            return [Change(pk=change.pk * 10) for change in super().changes_for(execution, op, result)]

    monkeypatch.setattr(settings, "viewsets_command_middleware", [Narrow()])
    router = APIRouter()
    collection_viewset(router)
    TestClient(app_for(router)).delete("/tracks/1")
    assert logged("/tracks:all") == [("records_changed", {"op": "deleted", "changes": changes(10)})]


def test_records_changed_is_an_event_named_records_changed_of_version_1():
    assert issubclass(RecordsChanged, Event)
    assert RecordsChanged.event_name == "records_changed"
    assert RecordsChanged.event_version == 1
    event = RecordsChanged(op="created", changes=[Change(pk=1)])
    assert event.model_dump(mode="json") == {"op": "created", "changes": [{"pk": 1}]}


# a celery_viewset action


def test_a_celery_viewset_action_publishes_in_the_worker_to_the_routed_topic(monkeypatch):
    executions: list[ActionExecution] = []

    class Recording(ChangeEvents):
        def changes_for(self, execution, op, result):
            executions.append(execution)
            return super().changes_for(execution, op, result)

    monkeypatch.setattr(settings, "viewsets_command_middleware", [Recording()])
    executor = _Executor(monkeypatch)

    class Base:
        __router = APIRouter()
        pk_field_name = "id"

        @__router.post("")
        async def create(self, data: Track) -> Track:
            return Track(id=7, title=data.title)

    worker = celery_viewset_server(celery_app=executor.server_app, task_prefix="things")(type("Worker", (Base,), {}))
    client = celery_viewset_client(celery_app=executor.client_app, task_prefix="things", redis_client=MagicMock())(
        type("Client", (Base,), {})
    )
    router = APIRouter()
    route_viewset(router, "/things", pk_field_name="id")(client)
    response = TestClient(app_for(router, prefix="/api")).post("/api/things", json={"title": "Seven"})
    assert response.json() == {"id": 7, "title": "Seven"}

    assert executor.sent[0]["_context"][TOPIC_CONTEXT_KEY] == "/api/things"
    [execution] = executions
    assert execution.viewset_class is worker
    assert not hasattr(worker, "__viewset_metadata__")
    assert logged("/api/things") == [("records_changed", {"op": "created", "changes": changes(7)})]


def _celery_pair(monkeypatch, decorators=()):
    executor = _Executor(monkeypatch)

    class Base:
        __router = APIRouter()
        pk_field_name = "id"

        @__router.post("")
        async def create(self, data: Track) -> Track:
            return Track(id=7, title=data.title)

    base = Base
    for decorator in decorators:
        base = decorator(base)
    worker = celery_viewset_server(celery_app=executor.server_app, task_prefix="things")(type("Worker", (base,), {}))
    client = celery_viewset_client(celery_app=executor.client_app, task_prefix="things", redis_client=MagicMock())(
        type("Client", (base,), {})
    )
    return executor, worker, client


def _orders_calling(client) -> APIRouter:
    """A viewset at /orders whose action `place` creates a thing through the celery client."""
    router = APIRouter()

    @route_viewset(router, "/orders", pk_field_name="id")
    class OrdersViewSet:
        __router = APIRouter()

        @__router.post("/place")
        async def place(self) -> Track:
            return await client().create(data=Track(title="Seven"))

    return router


def test_a_celery_action_dispatched_in_another_action_does_not_publish_to_that_actions_topic(monkeypatch):
    executor, _worker, client = _celery_pair(monkeypatch)
    response = TestClient(app_for(_orders_calling(client), prefix="/api"), raise_server_exceptions=False).post(
        "/api/orders/place"
    )
    assert executor.sent[0]["_context"][TOPIC_CONTEXT_KEY] == "/api/orders"
    assert executor.sent[0]["_parent"]["action"] == "place"
    assert response.status_code >= 500
    assert logged("/api/orders") == []


def test_a_celery_action_dispatched_in_another_action_publishes_to_its_configured_topic(monkeypatch):
    _executor, _worker, client = _celery_pair(
        monkeypatch, decorators=[action_configuration({ChangeEvents: {"topic": "/api/things"}})]
    )
    response = TestClient(app_for(_orders_calling(client), prefix="/api")).post("/api/orders/place")
    assert response.json() == {"id": 7, "title": "Seven"}
    assert logged("/api/things") == [("records_changed", {"op": "created", "changes": changes(7)})]
    assert logged("/api/orders") == []


def test_the_topic_of_an_execution_dispatched_in_another_action_is_the_base_path():
    router = APIRouter()
    viewset_class = collection_viewset(router)
    execution = ActionExecution(
        viewset_class,
        viewset_class(),
        "destroy",
        (),
        {"pk": 1},
        Context({TOPIC_CONTEXT_KEY: "/api/orders"}),
        None,
        ExecutionRef("app.OrdersViewSet", "place"),
    )
    assert ChangeEvents().topic_for(execution) == "/tracks"

    class Worker:
        pass

    nested = ActionExecution(Worker, Worker(), "destroy", (), {}, execution.context, None, execution.parent)
    with pytest.raises(TypeError, match="dispatched in app.OrdersViewSet.place"):
        ChangeEvents().topic_for(nested)
