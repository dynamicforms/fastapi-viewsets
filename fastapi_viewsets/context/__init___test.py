import asyncio
import datetime
import json

from typing import Any
from unittest.mock import MagicMock

import pytest

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_viewsets.conf import settings
from fastapi_viewsets.context import (
    build_context,
    ByAction,
    Context,
    deserialize_context,
    LazyObject,
    SerializableObject,
    serialize_context,
)
from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.decorators.celery_viewset import celery_viewset_client
from fastapi_viewsets.decorators.celery_viewset.server import _reconstruct_kwargs
from fastapi_viewsets.mixins import ListMixin


@pytest.fixture(autouse=True)
def reset_settings():
    """Context processors/encoders/middleware are global settings - keep tests isolated."""
    yield
    settings.viewsets_context_processors = []
    settings.viewsets_context_json_encoder = None
    settings.viewsets_context_json_decoder = None
    settings.viewsets_command_middleware = []


# ---------------------------------------------------------------------------
# Module-level SerializableObject/LazyObject fixtures.
#
# NOTE: SerializableObject subclasses must be importable at module level - the type tag embedded
# by serialize_context() is "module.QualName", resolved back via importlib.import_module() + a
# plain getattr(), not a registry. A class defined inside a test function (qualname containing
# "<locals>") would not be importable this way.
# ---------------------------------------------------------------------------


class _Tagged(SerializableObject):
    """Trivial concrete SerializableObject: __serialize__/__deserialize__ round-trip `value` as-is."""

    def __serialize__(self) -> Any:
        return self.value

    @classmethod
    def __deserialize__(cls, data: Any) -> "_Tagged":
        return cls(data)


_FAKE_SESSION_STORE = {"sess-1": "jure"}


class _SessionUser(LazyObject):
    """
    Mimics the auth use case: carries a cheap session_id (not the resolved user) across
    serialization, resolves the actual user lazily (and only if something actually awaits it).

    resolve() is sync and returns a coroutine (the recommended pattern for async work).
    """

    def __init__(self, session_id: str):
        super().__init__()
        self.session_id = session_id

    def serialize_recipe(self) -> Any:
        return {"session_id": self.session_id}

    @classmethod
    def deserialize_recipe(cls, data: Any) -> "_SessionUser":
        return cls(data["session_id"])

    def resolve(self) -> Any:
        return self._fetch_user()

    async def _fetch_user(self) -> Any:
        return _FAKE_SESSION_STORE.get(self.session_id)


class _SyncCounter(LazyObject):
    """A LazyObject whose resolve() is purely sync (no I/O)."""

    def __init__(self, compute):
        super().__init__()
        self._compute = compute

    def serialize_recipe(self) -> Any:
        return None

    @classmethod
    def deserialize_recipe(cls, data: Any) -> "_SyncCounter":
        raise NotImplementedError("not needed for this test")

    def resolve(self) -> Any:
        return self._compute()


# ---------------------------------------------------------------------------
# SerializableObject
# ---------------------------------------------------------------------------


def test_serializable_object_is_abstract():
    with pytest.raises(TypeError):
        SerializableObject(value=1)


def test_serializable_object_value_holds_wrapped_value():
    obj = _Tagged(42)
    assert obj.value == 42


@pytest.mark.asyncio
async def test_serializable_object_is_awaitable_even_though_eager():
    """Uniform API: even a plain, already-known SerializableObject value is awaitable, so callers
    never need to branch on eager vs. lazy."""
    obj = _Tagged(42)
    assert await obj == 42


# ---------------------------------------------------------------------------
# LazyObject
# ---------------------------------------------------------------------------


def test_lazy_object_is_abstract():
    with pytest.raises(TypeError):
        LazyObject()


def test_lazy_object_value_before_any_await_raises():
    obj = _SessionUser("sess-1")
    with pytest.raises(RuntimeError, match="accessed before it was awaited"):
        _ = obj.value
    assert obj.is_resolved is False


@pytest.mark.asyncio
async def test_lazy_object_await_gives_the_real_value_directly():
    """await obj (async resolve() mode) gives the actual resolved value, not the wrapper - the
    same ergonomics as Django's request.auser(): one explicit await, then it's a plain object."""
    obj = _SessionUser("sess-1")
    resolved = await obj
    assert resolved == "jure"
    assert obj.value == "jure"  # now a cheap peek, already resolved
    assert obj.is_resolved is True


@pytest.mark.asyncio
async def test_lazy_object_sync_resolve_also_works_via_await():
    calls = []

    def compute():
        calls.append(1)
        return "computed"

    obj = _SyncCounter(compute)
    assert obj.is_resolved is False
    assert await obj == "computed"
    assert obj.value == "computed"
    assert await obj == "computed"  # second await, no re-trigger
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_lazy_object_concurrent_await_resolves_exactly_once():
    """Regression test: concurrent awaiters before resolution must share one Future/Task, not
    each independently trigger resolve() (a race condition in an earlier hand-rolled version)."""
    calls = []

    class CountingLazy(LazyObject):
        def serialize_recipe(self):
            return None

        @classmethod
        def deserialize_recipe(cls, _data):
            return cls()

        def resolve(self):
            calls.append(1)
            return self._compute()

        async def _compute(self):
            await asyncio.sleep(0.01)
            return "computed"

    obj = CountingLazy()
    first, second = await asyncio.gather(obj, obj)
    assert first == second == "computed"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_lazy_object_unresolved_value_never_computed_if_unused():
    calls = []

    class TrackedLazy(LazyObject):
        def serialize_recipe(self):
            return None

        @classmethod
        def deserialize_recipe(cls, _data):
            return cls()

        def resolve(self):
            calls.append(1)
            return "computed"

    TrackedLazy()  # constructed, never awaited
    assert calls == []


# ---------------------------------------------------------------------------
# serialize_context / deserialize_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serialize_context_passes_plain_values_through():
    ctx = {"a": 1, "b": "text", "c": None}
    assert await serialize_context(ctx) == ctx


@pytest.mark.asyncio
async def test_serialize_context_awaits_bare_awaitables_not_wrapped_in_serializableobject():
    """A processor that puts a raw coroutine/Future directly into context (not wrapped in
    SerializableObject) still gets a plain, JSON-safe value out of serialize_context."""

    async def _compute():
        return 99

    serialized = await serialize_context({"n": _compute()})
    assert serialized == {"n": 99}


@pytest.mark.asyncio
async def test_serialize_context_tags_serializable_objects():
    ctx = {"n": _Tagged(7)}
    serialized = await serialize_context(ctx)
    assert serialized["n"]["__fpv_type__"] == f"{_Tagged.__module__}.{_Tagged.__qualname__}"
    assert serialized["n"]["__fpv_value__"] == 7


@pytest.mark.asyncio
async def test_deserialize_context_reconstructs_serializable_objects():
    ctx = {"n": _Tagged(7)}
    restored = deserialize_context(await serialize_context(ctx))
    assert isinstance(restored["n"], _Tagged)
    assert restored["n"].value == 7


@pytest.mark.asyncio
async def test_lazy_object_survives_serialize_deserialize_unresolved_and_resolves_after():
    """The serialized form carries the resolution inputs (session_id), not the resolved value -
    the reconstructed object is unresolved and only computes its value when awaited."""
    ctx = {"user": _SessionUser("sess-1")}
    serialized = await serialize_context(ctx)
    assert serialized["user"]["__fpv_value__"] == {"recipe": {"session_id": "sess-1"}}

    restored = deserialize_context(serialized)
    user_obj = restored["user"]
    assert isinstance(user_obj, _SessionUser)
    assert user_obj.is_resolved is False

    resolved = await user_obj
    assert resolved == "jure"


@pytest.mark.asyncio
async def test_lazy_object_already_resolved_survives_with_shortcut_no_reresolution():
    """If already resolved before serialization, the resolved value travels alongside the recipe
    (not instead of it) - deserialize pre-seeds the new instance so it doesn't need to re-run
    resolution for something already known, without sharing the mutable Python object."""
    obj = _SessionUser("sess-1")
    await obj  # resolve it once

    serialized = await serialize_context({"user": obj})
    assert serialized["user"]["__fpv_value__"]["recipe"] == {"session_id": "sess-1"}
    assert serialized["user"]["__fpv_value__"]["resolved_value"] == "jure"

    restored = deserialize_context(serialized)["user"]
    assert restored is not obj  # fully independent instance
    assert restored.is_resolved is True
    assert restored.value == "jure"  # no await needed - synchronous peek works immediately


def test_deserialize_context_unknown_type_tag_raises():
    with pytest.raises(TypeError):
        deserialize_context({"n": {"__fpv_type__": "os.path.NotASerializableObject", "__fpv_value__": 1}})


def test_deserialize_context_is_loop_independent():
    """Reconstruction must never require a running event loop - it happens synchronously in
    celery_viewset_server's _reconstruct_kwargs, before the worker's event loop starts."""
    data = {
        "user": {
            "__fpv_type__": f"{_SessionUser.__module__}.{_SessionUser.__qualname__}",
            "__fpv_value__": {"recipe": {"session_id": "sess-1"}},
        }
    }
    restored = deserialize_context(data)  # no asyncio.run() anywhere around this call
    assert isinstance(restored["user"], _SessionUser)
    assert restored["user"].is_resolved is False


class _DTEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, datetime.datetime):
            return {"__dt__": o.isoformat()}
        return super().default(o)


class _DTDecoder(json.JSONDecoder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, object_hook=self._hook, **kwargs)

    @staticmethod
    def _hook(d):
        if "__dt__" in d:
            return datetime.datetime.fromisoformat(d["__dt__"])
        return d


@pytest.mark.asyncio
async def test_serialize_context_uses_custom_json_encoder_for_plain_values():
    """Plain (non-SerializableObject) values that aren't natively JSON-safe - e.g. datetime -
    still go through settings.viewsets_context_json_encoder/decoder."""
    settings.viewsets_context_json_encoder = _DTEncoder
    settings.viewsets_context_json_decoder = _DTDecoder

    ts = datetime.datetime(2026, 1, 1, 12, 0, 0)
    serialized = await serialize_context({"ts": ts})
    assert serialized == {"ts": {"__dt__": "2026-01-01T12:00:00"}}

    restored = deserialize_context(serialized)
    assert restored["ts"] == ts


# ---------------------------------------------------------------------------
# Context controller
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_uniform_await_attribute_and_item_access():
    ctx = Context({"tz": "UTC", "user": _SessionUser("sess-1")})
    assert await ctx.tz == "UTC"
    assert await ctx["tz"] == "UTC"
    assert await ctx.user == "jure"
    assert await ctx["user"] == "jure"


def test_context_contains_and_len():
    ctx = Context({"a": 1, "b": 2})
    assert "a" in ctx
    assert "z" not in ctx
    assert len(ctx) == 2
    assert set(iter(ctx)) == {"a", "b"}


@pytest.mark.asyncio
async def test_context_clone_for_command_is_isolated_and_keeps_resolved_shortcut():
    obj = _SessionUser("sess-1")
    await obj  # resolve on the "connection-level" context

    ctx = Context({"user": obj})
    clone = await ctx.clone_for_command()

    assert clone is not ctx
    assert clone.raw()["user"] is not obj  # fully independent instance
    assert clone.raw()["user"].is_resolved is True  # ...but cheaply reused the resolved value
    assert await clone.user == "jure"


# ---------------------------------------------------------------------------
# ByAction / Context.configuration_for
# ---------------------------------------------------------------------------


def test_by_action_resolves_per_action_with_default_fallback():
    value = ByAction(list_items="read", update="write", default="none")
    assert value.resolve("list_items") == "read"
    assert value.resolve("update") == "write"
    assert value.resolve("destroy") == "none"  # falls back to default
    assert value.resolve(None) == "none"


def test_configuration_for_returns_none_when_nothing_configured():
    ctx = Context({}, action_configuration={}, action_name="list_items")
    assert ctx.configuration_for("anything") is None


def test_configuration_for_returns_plain_value_as_is():
    ctx = Context({}, action_configuration={"perm": "read-only"}, action_name="list_items")
    assert ctx.configuration_for("perm") == "read-only"


def test_configuration_for_resolves_byaction_against_current_action_name():
    value = ByAction(list_items="read", update="write")
    ctx = Context({}, action_configuration={"perm": value}, action_name="update")
    assert ctx.configuration_for("perm") == "write"


@pytest.mark.asyncio
async def test_clone_for_command_resolves_byaction_fresh_for_a_different_action():
    """Regression test: the raw (unresolved) action_configuration dict survives clone_for_command
    unchanged, so a clone representing a *different* action resolves ByAction values against its
    own action_name, rather than reusing whatever the original action resolved to."""
    value = ByAction(list_items="read", update="write", default="none")
    ctx = Context({}, action_configuration={"perm": value}, action_name="list_items")
    assert ctx.configuration_for("perm") == "read"

    clone = await ctx.clone_for_command(action_name="update")
    assert clone.configuration_for("perm") == "write"
    assert ctx.configuration_for("perm") == "read"  # original is untouched


@pytest.mark.asyncio
async def test_clone_for_command_defaults_to_the_same_action_name():
    value = ByAction(list_items="read", default="none")
    ctx = Context({}, action_configuration={"perm": value}, action_name="list_items")
    clone = await ctx.clone_for_command()
    assert clone.configuration_for("perm") == "read"


# ---------------------------------------------------------------------------
# build_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_context_merges_multiple_processors():
    async def proc_a(_request, _viewset):
        return {"a": 1, "shared": "from_a"}

    async def proc_b(_request, _viewset):
        return {"b": 2, "shared": "from_b"}

    settings.viewsets_context_processors = [proc_a, proc_b]

    ctx = await build_context(None, None)
    assert isinstance(ctx, Context)
    assert await ctx.a == 1
    assert await ctx.b == 2
    assert await ctx.shared == "from_b"  # last processor wins on collision


@pytest.mark.asyncio
async def test_build_context_with_no_processors_returns_empty_context():
    ctx = await build_context(None, None)
    assert len(ctx) == 0


@pytest.mark.asyncio
async def test_build_context_passes_request_and_viewset_to_processors():
    received = {}

    async def proc(request, viewset):
        received["request"] = request
        received["viewset"] = viewset
        return {}

    settings.viewsets_context_processors = [proc]
    sentinel_request = object()
    sentinel_viewset = object()

    await build_context(sentinel_request, sentinel_viewset)
    assert received["request"] is sentinel_request
    assert received["viewset"] is sentinel_viewset


@pytest.mark.asyncio
async def test_build_context_delivers_config_to_processors_declaring_it_by_name():
    async def wants_config(_request, _viewset, config) -> dict:
        return {"perm": config}

    settings.viewsets_context_processors = [wants_config]
    ctx = await build_context(None, None, action_configuration={wants_config: "read-only"})
    assert await ctx.perm == "read-only"


@pytest.mark.asyncio
async def test_build_context_resolves_byaction_config_against_action_name():
    async def wants_config(_request, _viewset, config) -> dict:
        return {"perm": config}

    settings.viewsets_context_processors = [wants_config]
    action_configuration = {wants_config: ByAction(update="write", default="read")}

    ctx = await build_context(None, None, action_configuration, action_name="update")
    assert await ctx.perm == "write"


@pytest.mark.asyncio
async def test_build_context_processor_without_config_param_is_unaffected():
    """A processor with the classic 2-arg signature is called exactly as before, even when
    action_configuration is non-empty - it just never receives it."""

    async def two_arg_processor(_request, _viewset) -> dict:
        return {"ok": True}

    settings.viewsets_context_processors = [two_arg_processor]
    ctx = await build_context(None, None, action_configuration={two_arg_processor: "ignored"})
    assert await ctx.ok is True


@pytest.mark.asyncio
async def test_build_context_config_defaults_to_none_when_not_configured():
    async def wants_config(_request, _viewset, config) -> dict:
        return {"perm": config}

    settings.viewsets_context_processors = [wants_config]
    ctx = await build_context(None, None)
    assert await ctx.perm is None


# ---------------------------------------------------------------------------
# route_viewset integration - context reaches perform_* via a live Request
# ---------------------------------------------------------------------------


class Item(BaseModel):
    id: int
    name: str


def test_route_viewset_builds_context_from_auth_header():
    async def auth_processor(request: Request, _viewset) -> dict:
        return {"user": request.headers.get("x-user", "anonymous")}

    settings.viewsets_context_processors = [auth_processor]

    router = APIRouter()

    @route_viewset(router, base_path="/items")
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, context: Context) -> list[Item]:
            return [Item(id=1, name=await context.user)]

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    authenticated = client.get("/items", headers={"x-user": "jure"})
    assert authenticated.json() == [{"id": 1, "name": "jure"}]

    anonymous = client.get("/items")
    assert anonymous.json() == [{"id": 1, "name": "anonymous"}]


def test_route_viewset_resolves_lazy_object_from_context():
    """A LazyObject placed in context by a processor is only resolved when perform_* awaits it."""

    async def session_processor(request: Request, _viewset) -> dict:
        return {"user": _SessionUser(request.headers.get("x-session", ""))}

    settings.viewsets_context_processors = [session_processor]

    router = APIRouter()

    @route_viewset(router, base_path="/items")
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, context: Context) -> list[Item]:
            user = await context.user
            return [Item(id=1, name=user or "anonymous")]

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/items", headers={"x-session": "sess-1"})
    assert response.json() == [{"id": 1, "name": "jure"}]


def test_custom_endpoint_without_context_param_is_unaffected():
    """A custom __router endpoint that doesn't declare a `context` param is untouched - no Request
    injected, no processors run - opt-in is per-route, exactly today's behaviour. The built-in
    mixin routes (list_items, create, ...) always declare context - see mixins.py - so this is
    only observable on a hand-written custom route."""
    settings.viewsets_context_processors = []  # even with processors configured...

    router = APIRouter()

    @route_viewset(router, base_path="/items")
    class ItemViewSet:
        __router = APIRouter()

        @__router.get("ping")
        async def ping(self) -> str:
            return "pong"

    ping_route = next(r for r in router.routes if r.path == "/items/ping" and "GET" in r.methods)
    import inspect

    sig_params = inspect.signature(ping_route.endpoint).parameters
    assert "context" not in sig_params
    assert "request" not in sig_params  # ...no Request is injected for this route


# ---------------------------------------------------------------------------
# celery client/server - context (incl. a LazyObject value) survives the boundary
# ---------------------------------------------------------------------------


def test_celery_context_round_trip_with_lazy_object():
    """Context built with a live Request (client side, via settings.viewsets_context_processors)
    is serialized for the Celery task payload and correctly reconstructed on the worker side - a
    LazyObject value travels as its unresolved recipe, not a precomputed value."""
    from fastapi_viewsets.decorators.celery_viewset import result_reader

    async def session_processor(request, _viewset) -> dict:
        return {"user": _SessionUser(request.headers.get("x-session", ""))}

    settings.viewsets_context_processors = [session_processor]

    celery_app = MagicMock()
    redis_mock = MagicMock()

    captured_kwargs = {}

    def fake_send_task(_name, args=None, kwargs=None):  # noqa: ARG001 - `args` must match send_task's kwarg name
        captured_kwargs.update(kwargs)

    celery_app.send_task.side_effect = fake_send_task

    original_register = result_reader.register_future

    def mock_register(_correlation_id, future):
        asyncio.get_event_loop().call_soon(lambda: future.set_result([]))

    result_reader.register_future = mock_register
    try:
        router = APIRouter()

        # Mirrors the documented combined-decorator sequence (see docs/guide/routers.md
        # "Combining both decorators" and demo/backend/viewsets.py + main.py): celery_viewset
        # first (patches instance methods to dispatch via Celery), route_viewset second, applied
        # as a plain function call on the *same* class (not stacked decorator syntax, which would
        # apply them in the wrong order).
        @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self, _context: Context) -> list[Item]:
                return []

        route_viewset(router, base_path="/items")(ItemViewSet)

        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            client.get("/items", headers={"x-session": "sess-1"})
    finally:
        result_reader.register_future = original_register

    # The context sent to Celery carries the LazyObject's serialized recipe, not a resolved value
    assert captured_kwargs["context"]["user"]["__fpv_value__"]["recipe"] == {"session_id": "sess-1"}

    # Worker-side reconstruction restores an unresolved _SessionUser that can still be resolved
    async def worker_endpoint(_self, context: Context):
        return context

    worker_endpoint.__name__ = "list_items"
    reconstructed = _reconstruct_kwargs(worker_endpoint, {"context": captured_kwargs["context"]})
    context_obj = reconstructed["context"]
    assert isinstance(context_obj, Context)
    user_obj = context_obj.raw()["user"]
    assert isinstance(user_obj, _SessionUser)
    assert user_obj.is_resolved is False
