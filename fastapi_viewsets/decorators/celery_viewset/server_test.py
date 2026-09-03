from datetime import date, datetime
from unittest.mock import MagicMock

import pytest

from pydantic import BaseModel

from fastapi_viewsets.decorators import (
    celery_viewset_server,
)
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

    @celery_viewset_server(celery_app=celery_app, task_prefix="items")
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
    JSON-serialize/store the raw return value (which may be a Pydantic model)."""
    celery_app = MagicMock()
    task_decorator = MagicMock()
    celery_app.task.return_value = task_decorator

    @celery_viewset_server(celery_app=celery_app, task_prefix="items")
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context) -> list[Item]:
            return [Item(id=1, name="test")]

    calls = celery_app.task.call_args_list
    assert calls
    assert all(call.kwargs.get("ignore_result") is True for call in calls)


def test_celery_viewset_server_execution():
    celery_app = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    @celery_viewset_server(celery_app=celery_app, task_prefix="items")
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context) -> list[Item]:
            return [Item(id=1, name="test")]

    assert "items.list_items" in registered_tasks
    sync_func = registered_tasks["items.list_items"]
    result = sync_func(context={})
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

    @celery_viewset_server(celery_app=celery_app, task_prefix="counter", lifecycle="per-request")
    class CounterViewSet(ListMixin[int]):
        def __init__(self):
            Counter.count += 1

        async def perform_list(self, _context) -> list[int]:
            return [Counter.count]

    sync_func = registered_tasks["counter.list_items"]
    res1 = sync_func(context={})
    assert res1 == [1]
    res2 = sync_func(context={})
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
    sync_func(context={}, _correlation_id="test-corr-id", _result_queue_key="celery_viewset_results:items")

    redis_mock.rpush.assert_called_once()
    call_args = redis_mock.rpush.call_args
    queue_key = call_args[0][0]
    payload = json.loads(call_args[0][1])
    assert queue_key == "celery_viewset_results:items"
    assert payload["correlation_id"] == "test-corr-id"
    assert payload["result"] is not None


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
    sync_func(context={}, _correlation_id="test-corr-id", _result_queue_key="celery_viewset_results:items")

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
    result = sync_func(context={}, _correlation_id="test-corr-id", _result_queue_key="celery_viewset_results:items")

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
    with pytest.raises(ValueError, match="DB connection failed"):
        sync_func(context={}, _correlation_id="err-corr-id", _result_queue_key="celery_viewset_results:items")

    redis_mock.rpush.assert_called_once()
    call_args = redis_mock.rpush.call_args
    payload = json.loads(call_args[0][1])
    assert payload["correlation_id"] == "err-corr-id"
    assert "error" in payload
    assert "DB connection failed" in payload["error"]


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
    result = sync_func(context={}, _correlation_id="http-corr-id", _result_queue_key="celery_viewset_results:items")
    assert result is None

    redis_mock.rpush.assert_called_once()
    payload = json.loads(redis_mock.rpush.call_args[0][1])
    assert payload["correlation_id"] == "http-corr-id"
    assert payload["http_status_code"] == 404
    assert payload["http_detail"] == "Item with pk 5 not found"
    assert "error" in payload


def test_celery_viewset_server_no_redis_no_push():
    """When redis_client is None, server does not attempt to push result."""
    celery_app = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    @celery_viewset_server(celery_app=celery_app, task_prefix="items")
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context) -> list[Item]:
            return [Item(id=1, name="test")]

    sync_func = registered_tasks["items.list_items"]
    result = sync_func(context={}, _correlation_id="some-id", _result_queue_key="celery_viewset_results:items")
    assert result == [Item(id=1, name="test")]


def test_celery_viewset_server_metadata():
    """Server decorator sets __celery_viewset_metadata__ on the class."""
    celery_app = MagicMock()

    @celery_viewset_server(celery_app=celery_app, task_prefix="myprefix")
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

    @celery_viewset_server(celery_app=celery_app, task_prefix="items")
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context) -> list[Item]:
            return [Item(id=42, name="server-item")]

    assert "items.list_items" in registered_tasks
    result = registered_tasks["items.list_items"](context={})
    assert result == [Item(id=42, name="server-item")]


# ---------------------------------------------------------------------------
# _reconstruct_kwargs tests
# ---------------------------------------------------------------------------


def test_reconstruct_kwargs_with_missing_required_field():
    """_reconstruct_kwargs should use model_construct fallback when required field (e.g. id) is missing."""
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs

    async def endpoint(data: Item) -> Item:
        return data

    # Simulate what client sends: dict without 'id' (autoinc field stripped for POST)
    kwargs = {"data": {"name": "new item"}}
    result = _reconstruct_kwargs(endpoint, kwargs)

    assert isinstance(result["data"], Item)
    assert result["data"].name == "new item"


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
    """_reconstruct_kwargs should use model_construct fallback when TypeVar resolves to Item but id is missing."""
    from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs
    from fastapi_viewsets.mixins import CreateMixin

    class ItemViewSet(CreateMixin[int, Item]):
        async def perform_create(self, _context, data: Item) -> Item:
            return data

    kwargs = {"data": {"name": "no id"}}
    result = _reconstruct_kwargs(CreateMixin.create, kwargs, cls=ItemViewSet)

    assert isinstance(result["data"], Item)
    assert result["data"].name == "no id"


# ---------------------------------------------------------------------------
# celery kwargs hook
# ---------------------------------------------------------------------------


def test_celery_kwargs_hook_none_preserves_existing_behavior():
    """With no hook registered, the sync wrapper behaves exactly as before."""
    from fastapi_viewsets.decorators.celery_viewset import server

    celery_app = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    assert server._celery_kwargs_hook is None

    @celery_viewset_server(celery_app=celery_app, task_prefix="items")
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context) -> list[Item]:
            return [Item(id=1, name="test")]

    sync_func = registered_tasks["items.list_items"]
    result = sync_func(context={})
    assert result == [Item(id=1, name="test")]


def test_celery_kwargs_hook_called_before_reconstruct_kwargs():
    """The hook receives raw kwargs, before _reconstruct_kwargs has turned dicts into models."""
    from fastapi_viewsets.decorators.celery_viewset import server
    from fastapi_viewsets.decorators.celery_viewset.server import set_celery_kwargs_hook

    celery_app = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    seen_kwargs = {}

    def hook(run, kwargs):
        seen_kwargs.update(kwargs)
        return run, kwargs

    set_celery_kwargs_hook(hook)
    try:

        @celery_viewset_server(celery_app=celery_app, task_prefix="items")
        class ItemViewSet(CreateMixin[int, Item]):
            async def perform_create(self, _context, data: Item) -> Item:
                return data

        create_func = registered_tasks["items.create"]
        create_func(context={}, data={"id": 1, "name": "raw"})

        # If the hook ran after reconstruction, this would be an Item instance, not a dict.
        assert seen_kwargs["data"] == {"id": 1, "name": "raw"}
    finally:
        set_celery_kwargs_hook(None)
        assert server._celery_kwargs_hook is None


def test_celery_kwargs_hook_can_replace_runner_and_consume_kwargs():
    """The hook may swap out the runner and strip a kwarg the endpoint never declared."""
    from fastapi_viewsets.decorators.celery_viewset.server import set_celery_kwargs_hook

    celery_app = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    calls = []

    def hook(run, kwargs):
        kwargs = dict(kwargs)
        kwargs.pop("_extra_field", None)

        def wrapped_run(coro):
            calls.append("wrapped_run")
            return run(coro)

        return wrapped_run, kwargs

    set_celery_kwargs_hook(hook)
    try:

        @celery_viewset_server(celery_app=celery_app, task_prefix="items")
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self, _context) -> list[Item]:
                return [Item(id=1, name="test")]

        sync_func = registered_tasks["items.list_items"]
        result = sync_func(context={}, _extra_field="unused-by-endpoint")

        assert result == [Item(id=1, name="test")]
        assert calls == ["wrapped_run"]
    finally:
        set_celery_kwargs_hook(None)


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
