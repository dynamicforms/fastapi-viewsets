from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from pydantic import BaseModel, Field, field_validator, ValidationError

from fastapi_viewsets.decorators import (
    celery_viewset_server,
)
from fastapi_viewsets.decorators.celery_viewset.result_reader import RESULT_QUEUE_TTL
from fastapi_viewsets.exceptions import ActionFailedError
from fastapi_viewsets.mixins import CreateMixin, ListMixin


class Item(BaseModel):
    id: int
    name: str


class ItemWithDate(BaseModel):
    id: int
    created: date
    updated: datetime


# ---------------------------------------------------------------------------
# celery_viewset_server tests
# ---------------------------------------------------------------------------


def test_celery_viewset_server_decorator_registration():
    celery_app = MagicMock()
    task_decorator = MagicMock()
    celery_app.task.return_value = task_decorator

    @celery_viewset_server(celery_app=celery_app, task_prefix="items", redis_client=MagicMock())
    class ItemViewSet(ListMixin[Item], CreateMixin[int, Item]):
        async def perform_list(self, _context) -> list[Item]:
            return [Item(id=1, name="test")]

        async def perform_create(self, _context, data: Item) -> Item:
            return data

    calls = celery_app.task.call_args_list
    task_names = [call.kwargs.get("name") for call in calls]
    assert "items.list_items" in task_names
    assert "items.create" in task_names
    assert len(task_names) >= 2


def test_celery_viewset_server_registers_tasks_with_ignore_result():
    """celery_viewset_client never reads Celery's own result backend (it awaits the redis_client
    queue in client.py instead) - ignore_result=True must be set so Celery does not also try to
    JSON-serialize/store the raw return value (which may be a Pydantic model). acks_late=False and
    reject_on_worker_lost=False override the application's settings, so a call is never delivered a
    second time; store_errors_even_if_ignored=False stores no failure in the unused backend."""
    celery_app = MagicMock()
    task_decorator = MagicMock()
    celery_app.task.return_value = task_decorator

    @celery_viewset_server(celery_app=celery_app, task_prefix="items", redis_client=MagicMock())
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context) -> list[Item]:
            return [Item(id=1, name="test")]

    calls = celery_app.task.call_args_list
    assert calls
    for call in calls:
        options = {
            name: call.kwargs.get(name)
            for name in ("ignore_result", "acks_late", "reject_on_worker_lost", "store_errors_even_if_ignored")
        }
        assert options == {
            "ignore_result": True,
            "acks_late": False,
            "reject_on_worker_lost": False,
            "store_errors_even_if_ignored": False,
        }


def test_celery_viewset_server_execution():
    celery_app = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    @celery_viewset_server(celery_app=celery_app, task_prefix="items", redis_client=MagicMock())
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context) -> list[Item]:
            return [Item(id=1, name="test")]

    assert "items.list_items" in registered_tasks
    sync_func = registered_tasks["items.list_items"]
    result = sync_func()
    assert result == [Item(id=1, name="test")]


def test_celery_viewset_server_lifecycle_per_request():
    celery_app = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    class Counter:
        count = 0

    @celery_viewset_server(
        celery_app=celery_app, task_prefix="counter", lifecycle="per-request", redis_client=MagicMock()
    )
    class CounterViewSet(ListMixin[int]):
        def __init__(self):
            Counter.count += 1

        async def perform_list(self, _context) -> list[int]:
            return [Counter.count]

    sync_func = registered_tasks["counter.list_items"]
    res1 = sync_func()
    assert res1 == [1]
    res2 = sync_func()
    assert res2 == [2]


def test_celery_viewset_server_pushes_result_to_redis():
    """When redis_client is provided, server pushes result to Redis queue."""
    import json

    celery_app = MagicMock()
    redis_mock = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    @celery_viewset_server(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context) -> list[Item]:
            return [Item(id=1, name="test")]

    sync_func = registered_tasks["items.list_items"]
    sync_func(_correlation_id="test-corr-id", _result_queue_key="celery_viewset_results:items")

    redis_mock.rpush.assert_called_once()
    call_args = redis_mock.rpush.call_args
    queue_key = call_args[0][0]
    payload = json.loads(call_args[0][1])
    assert queue_key == "celery_viewset_results:items"
    assert payload["correlation_id"] == "test-corr-id"
    assert payload["result"] is not None
    redis_mock.expire.assert_called_once_with("celery_viewset_results:items", RESULT_QUEUE_TTL)


def test_celery_viewset_server_pushes_result_with_date_field_to_redis():
    """Result models with date/datetime fields must round-trip through the Redis JSON push without crashing."""
    import json

    celery_app = MagicMock()
    redis_mock = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    @celery_viewset_server(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
    class ItemViewSet(ListMixin[ItemWithDate]):
        async def perform_list(self, _context) -> list[ItemWithDate]:
            return [ItemWithDate(id=1, created=date(2026, 8, 1), updated=datetime(2026, 8, 1, 12, 30))]

    sync_func = registered_tasks["items.list_items"]
    sync_func(_correlation_id="test-corr-id", _result_queue_key="celery_viewset_results:items")

    redis_mock.rpush.assert_called_once()
    payload = json.loads(redis_mock.rpush.call_args[0][1])
    assert payload["result"] == [{"id": 1, "created": "2026-08-01", "updated": "2026-08-01T12:30:00"}]


def test_celery_viewset_server_returns_json_trivial_value_once_pushed_to_redis():
    """Once the real result is on the Redis queue, the task's own retval must not be the raw
    endpoint result - celery_viewset_client never reads it, and Celery itself would otherwise try
    to encode it (result backend, task-succeeded event, ...) even with ignore_result=True set."""
    celery_app = MagicMock()
    redis_mock = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    @celery_viewset_server(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context) -> list[Item]:
            return [Item(id=1, name="test")]

    sync_func = registered_tasks["items.list_items"]
    result = sync_func(_correlation_id="test-corr-id", _result_queue_key="celery_viewset_results:items")

    assert result is True
    redis_mock.rpush.assert_called_once()


def test_celery_viewset_server_pushes_error_to_redis_on_exception():
    """When task raises, server pushes error payload to Redis."""
    import json

    celery_app = MagicMock()
    redis_mock = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    @celery_viewset_server(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context) -> list[Item]:
            raise ValueError("DB connection failed")

    sync_func = registered_tasks["items.list_items"]
    with pytest.raises(ActionFailedError) as raised:
        sync_func(_correlation_id="err-corr-id", _result_queue_key="celery_viewset_results:items")
    assert isinstance(raised.value.__cause__, ValueError)

    redis_mock.rpush.assert_called_once()
    call_args = redis_mock.rpush.call_args
    payload = json.loads(call_args[0][1])
    assert payload["correlation_id"] == "err-corr-id"
    assert payload["error"]["class"] == "ActionFailedError"
    assert payload["error"]["params"] == {"task": "items.list_items", "reason": "error"}
    assert "DB connection failed" not in json.dumps(payload)


def test_celery_viewset_server_pushes_http_exception_to_redis():
    """When task raises HTTPException, server pushes http_status_code and http_detail to Redis."""
    import json

    from fastapi import HTTPException

    celery_app = MagicMock()
    redis_mock = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    @celery_viewset_server(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context) -> list[Item]:
            raise HTTPException(status_code=404, detail="Item with pk 5 not found")

    sync_func = registered_tasks["items.list_items"]
    result = sync_func(_correlation_id="http-corr-id", _result_queue_key="celery_viewset_results:items")
    assert result is None

    redis_mock.rpush.assert_called_once()
    payload = json.loads(redis_mock.rpush.call_args[0][1])
    assert payload["correlation_id"] == "http-corr-id"
    assert payload["http_status_code"] == 404
    assert payload["http_detail"] == "Item with pk 5 not found"
    assert "error" in payload


def test_celery_viewset_server_requires_a_redis_client():
    with pytest.raises(ValueError, match="redis_client"):
        celery_viewset_server(celery_app=MagicMock(), task_prefix="items")


def test_celery_viewset_server_metadata():
    """Server decorator sets __celery_viewset_metadata__ on the class."""
    celery_app = MagicMock()

    @celery_viewset_server(celery_app=celery_app, task_prefix="myprefix", redis_client=MagicMock())
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context) -> list[Item]:
            return []

    meta = ItemViewSet.__celery_viewset_metadata__
    assert meta["task_prefix"] == "myprefix"
    assert meta["celery_app"] is celery_app


# ---------------------------------------------------------------------------
# server _to_jsonable tests
# ---------------------------------------------------------------------------


def test_to_jsonable_with_pydantic_model():
    """_to_jsonable converts Pydantic model to dict."""
    from fastapi_viewsets.decorators.celery_viewset.server import _to_jsonable

    item = Item(id=1, name="test")
    result = _to_jsonable(item)
    assert result == {"id": 1, "name": "test"}


def test_to_jsonable_with_list_of_models():
    """_to_jsonable converts list of Pydantic models to list of dicts."""
    from fastapi_viewsets.decorators.celery_viewset.server import _to_jsonable

    items = [Item(id=1, name="a"), Item(id=2, name="b")]
    result = _to_jsonable(items)
    assert result == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_to_jsonable_with_date_field():
    """_to_jsonable serializes date/datetime fields to JSON-safe strings (mode="json")."""
    import json

    from fastapi_viewsets.decorators.celery_viewset.server import _to_jsonable

    item = ItemWithDate(id=1, created=date(2026, 8, 1), updated=datetime(2026, 8, 1, 12, 30))
    result = _to_jsonable(item)

    assert result == {"id": 1, "created": "2026-08-01", "updated": "2026-08-01T12:30:00"}
    # Must not raise - this is the actual celery round-trip failure mode.
    json.dumps(result)


def test_to_jsonable_with_plain_value():
    """_to_jsonable returns plain values unchanged."""
    from fastapi_viewsets.decorators.celery_viewset.server import _to_jsonable

    assert _to_jsonable(42) == 42
    assert _to_jsonable("hello") == "hello"
    assert _to_jsonable(None) is None
    assert _to_jsonable([1, 2, 3]) == [1, 2, 3]


def test_to_jsonable_with_viewset_result():
    """A worker-side action may return a ViewSetResult directly (see
    fastapi_viewsets.middleware.ViewSetResult) - _to_jsonable must tag it (it's a
    SerializableObject) rather than crash on a plain dataclass json.dumps can't handle."""
    import json

    from fastapi_viewsets.decorators.celery_viewset.server import _to_jsonable
    from fastapi_viewsets.middleware import ViewSetResult

    result = ViewSetResult(body=Item(id=1, name="test"), status_code=302, headers={"Location": "/x"})
    tagged = _to_jsonable(result)

    assert tagged["__fpv_type__"] == "fastapi_viewsets.middleware.ViewSetResult"
    assert tagged["__fpv_value__"] == {
        "body": {"id": 1, "name": "test"},
        "headers": {"Location": "/x"},
        "cookies": {},
        "status_code": 302,
    }
    json.dumps(tagged)  # must not raise - this is the actual celery round-trip failure mode


def test_celery_viewset_server_pushes_viewset_result_to_redis():
    """A worker-side custom endpoint returning ViewSetResult directly (see ViewSetResult in
    fastapi_viewsets.middleware) survives the push to Redis - see test_to_jsonable_with_viewset_result
    for the tagging itself. Uses a custom __router endpoint, not a mixin hook like perform_list:
    ListMixin.list_items itself expects perform_list to return plain records (it still does its own
    pagination/shaping around them), so ViewSetResult is only meaningful as the actual route
    endpoint's own return value."""
    import json

    from fastapi import APIRouter

    from fastapi_viewsets.middleware import ViewSetResult

    celery_app = MagicMock()
    redis_mock = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    @celery_viewset_server(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
    class ItemViewSet:
        __router = APIRouter()

        @__router.post("go")
        async def go(self, context) -> ViewSetResult[Item]:  # noqa: ARG002 - the context is injected by name
            return ViewSetResult(body=Item(id=1, name="test"), status_code=200)

    sync_func = registered_tasks["items.go"]
    sync_func(_correlation_id="test-corr-id", _result_queue_key="celery_viewset_results:items")

    payload = json.loads(redis_mock.rpush.call_args[0][1])
    assert payload["result"]["__fpv_type__"] == "fastapi_viewsets.middleware.ViewSetResult"
    assert payload["result"]["__fpv_value__"]["body"] == {"id": 1, "name": "test"}


# ---------------------------------------------------------------------------
# FastAPI integration - server
# ---------------------------------------------------------------------------


def test_fastapi_server_endpoint_executes_directly():
    """FastAPI app with celery_viewset_server can still be called directly (sync task)."""
    celery_app = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    @celery_viewset_server(celery_app=celery_app, task_prefix="items", redis_client=MagicMock())
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context) -> list[Item]:
            return [Item(id=42, name="server-item")]

    assert "items.list_items" in registered_tasks
    result = registered_tasks["items.list_items"]()
    assert result == [Item(id=42, name="server-item")]


# ---------------------------------------------------------------------------
# _reconstruct_kwargs tests
# ---------------------------------------------------------------------------


def test_reconstruct_kwargs_with_missing_required_field():
    """A create body arrives without its primary key and is reconstructed as the model without it."""
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs

    async def endpoint(data: Item) -> Item:
        return data

    # Simulate what client sends: dict without 'id' (autoinc field stripped for POST)
    kwargs = {"data": {"name": "new item"}}
    result = _reconstruct_kwargs(endpoint, kwargs)

    assert type(result["data"]) is Item
    assert result["data"].name == "new item"
    assert result["data"].model_fields_set == {"name"}


def test_reconstruct_kwargs_with_typevar_resolved_via_cls():
    """_reconstruct_kwargs should resolve TypeVar T to Item when cls is provided."""
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs
    from fastapi_viewsets.mixins import CreateMixin

    class ItemViewSet(CreateMixin[int, Item]):
        async def perform_create(self, _context, data: Item) -> Item:
            return data

    # CreateMixin.create has `data: T` — TypeVar, not Item
    kwargs = {"data": {"id": 1, "name": "via typevar"}}
    result = _reconstruct_kwargs(CreateMixin.create, kwargs, cls=ItemViewSet)

    assert isinstance(result["data"], Item)
    assert result["data"].id == 1
    assert result["data"].name == "via typevar"


def test_reconstruct_kwargs_with_typevar_missing_field():
    """A create body without its primary key is reconstructed when the TypeVar resolves to Item."""
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs
    from fastapi_viewsets.mixins import CreateMixin

    class ItemViewSet(CreateMixin[int, Item]):
        async def perform_create(self, _context, data: Item) -> Item:
            return data

    kwargs = {"data": {"name": "no id"}}
    result = _reconstruct_kwargs(CreateMixin.create, kwargs, cls=ItemViewSet)

    assert isinstance(result["data"], Item)
    assert result["data"].name == "no id"


def test_reconstruct_kwargs_with_full_model():
    """_reconstruct_kwargs should use model_validate when all fields are present."""
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs

    async def endpoint(data: Item) -> Item:
        return data

    kwargs = {"data": {"id": 5, "name": "full item"}}
    result = _reconstruct_kwargs(endpoint, kwargs)

    assert isinstance(result["data"], Item)
    assert result["data"].id == 5
    assert result["data"].name == "full item"


def test_reconstruct_kwargs_with_list_of_models():
    """A `list[Model]` kwarg reconstructs each element, not just a bare `Model` kwarg."""
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs

    async def endpoint(data: list[Item]) -> list[Item]:
        return data

    kwargs = {"data": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]}
    result = _reconstruct_kwargs(endpoint, kwargs)

    assert all(isinstance(item, Item) for item in result["data"])
    assert [item.name for item in result["data"]] == ["a", "b"]


def test_reconstruct_kwargs_with_optional_list_of_models():
    """An `Optional[list[Model]]` kwarg reconstructs each element too."""
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs

    async def endpoint(data: list[Item] | None = None) -> list[Item] | None:
        return data

    kwargs = {"data": [{"id": 1, "name": "a"}]}
    result = _reconstruct_kwargs(endpoint, kwargs)

    assert isinstance(result["data"][0], Item)
    assert result["data"][0].name == "a"


def test_reconstruct_kwargs_with_none_defaulted_optional_model():
    """A None-defaulted parameter reconstructs from a dict just like a required one.

    Before Python 3.11, `get_type_hints()` implicitly wraps a None-defaulted parameter's annotation
    in `Optional[...]`, so `data: Item = None` arrives here as `Union[Item, None]` rather than
    `Item` - which must still be recognised as a BaseModel hint.
    """
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs

    async def endpoint(data: Item = None) -> Item:
        return data

    kwargs = {"data": {"id": 5, "name": "full item"}}
    result = _reconstruct_kwargs(endpoint, kwargs)

    assert isinstance(result["data"], Item)
    assert result["data"].id == 5
    assert result["data"].name == "full item"


class Described(BaseModel):
    id: int = 0
    name: str
    description: str | None = None


def test_reconstruct_kwargs_of_a_partial_update_holds_the_stated_fields():
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs

    async def partial_update(pk: int, data: Described) -> Described:  # noqa: ARG001 - the mixin signature
        return data

    result = _reconstruct_kwargs(partial_update, {"pk": 1, "data": {"name": "x"}})
    assert isinstance(result["data"], Described)
    assert result["data"].model_dump(exclude_unset=True) == {"name": "x"}


def test_reconstruct_kwargs_of_a_bulk_partial_update_holds_the_stated_fields():
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs

    async def bulk_partial_update(data: dict[int, Described]) -> list[Described]:
        return list(data.values())

    result = _reconstruct_kwargs(bulk_partial_update, {"data": {"1": {"name": "x"}, "2": {"description": "d"}}})
    assert {pk: item.model_dump(exclude_unset=True) for pk, item in result["data"].items()} == {
        1: {"name": "x"},
        2: {"description": "d"},
    }


class Owner(BaseModel):
    name: str


class Pet(BaseModel):
    id: int
    name: str = Field(min_length=3, alias="title")
    owner: Owner

    @field_validator("name")
    @classmethod
    def stripped_name(cls, value):
        return value.strip()


async def create_pet(data: Pet) -> Pet:
    return data


def test_reconstruct_kwargs_validates_a_create_body_without_its_pk():
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs

    result = _reconstruct_kwargs(create_pet, {"data": {"name": " Rex ", "owner": {"name": "a"}}})
    assert type(result["data"]) is Pet
    assert result["data"].name == "Rex"
    assert result["data"].owner == Owner(name="a")
    assert "id" not in result["data"].__dict__


def test_reconstruct_kwargs_reads_a_model_by_field_name():
    """The FastAPI side dumps a model by field name, an aliased field included."""
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs

    result = _reconstruct_kwargs(create_pet, {"data": {"id": 1, "name": "Rex", "owner": {"name": "a"}}})
    assert result["data"] == Pet(id=1, title="Rex", owner=Owner(name="a"))


@pytest.mark.parametrize(
    "data",
    [
        {"name": "R", "owner": {"name": "a"}},
        {"name": "Rex"},
        {"owner": {"name": "a"}},
    ],
)
def test_reconstruct_kwargs_raises_for_an_invalid_model(data):
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs

    with pytest.raises(ValidationError):
        _reconstruct_kwargs(create_pet, {"data": data})


def test_reconstruct_kwargs_of_a_partial_update_reads_an_aliased_field_by_name():
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs

    async def partial_update(pk: int, data: Pet) -> Pet:  # noqa: ARG001 - the mixin signature
        return data

    result = _reconstruct_kwargs(partial_update, {"pk": 1, "data": {"name": " Rex "}})
    assert result["data"].model_dump(exclude_unset=True) == {"name": "Rex"}


def test_celery_viewset_server_pushes_request_validation_error_as_422():
    import json

    from fastapi.exceptions import RequestValidationError

    celery_app = MagicMock()
    redis_mock = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    errors = [{"type": "int_parsing", "loc": ("query", "year__in", 1), "msg": "not an int", "input": "abc"}]

    @celery_viewset_server(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context) -> list[Item]:
            raise RequestValidationError(errors)

    result = registered_tasks["items.list_items"](
        _correlation_id="corr", _result_queue_key="celery_viewset_results:items"
    )
    assert result is None
    payload = json.loads(redis_mock.rpush.call_args[0][1])
    assert payload["http_status_code"] == 422
    assert payload["http_detail"][0]["loc"] == ["query", "year__in", 1]
    assert payload["error"]["class"] == "RequestValidationError"
