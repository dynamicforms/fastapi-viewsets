import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_viewsets.action_configuration import (
    action_configuration,
    extra_middlewares_for,
    resolve_action_configuration,
)
from fastapi_viewsets.conf import settings
from fastapi_viewsets.context import ByAction, Context
from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.middleware import Middleware
from fastapi_viewsets.mixins import ListMixin


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    settings.viewsets_context_processors = []
    settings.viewsets_command_middleware = []
    settings.default_action_configuration = {}


# ---------------------------------------------------------------------------
# @action_configuration decorator + resolve_action_configuration merge order
# ---------------------------------------------------------------------------

_MARKER = object()


def test_decorator_works_identically_on_class_and_function():
    @action_configuration({_MARKER: "class-value"})
    class Foo:
        pass

    @action_configuration({_MARKER: "func-value"})
    def bar():
        pass

    assert Foo.__action_configuration__ == {_MARKER: "class-value"}
    assert bar.__action_configuration__ == {_MARKER: "func-value"}


def test_resolve_uses_global_default_when_nothing_else_configured():
    settings.default_action_configuration = {_MARKER: "default-value"}

    class Plain:
        pass

    assert resolve_action_configuration(Plain(), "list_items") == {_MARKER: "default-value"}


def test_resolve_class_level_overrides_global_default():
    settings.default_action_configuration = {_MARKER: "default-value"}

    @action_configuration({_MARKER: "class-value"})
    class Foo:
        pass

    assert resolve_action_configuration(Foo(), "list_items") == {_MARKER: "class-value"}


def test_resolve_method_level_overrides_class_level_per_key_only():
    """A method-level override replaces just its own key - other class-level keys still apply."""
    other = object()

    @action_configuration({_MARKER: "class-value", other: "class-other"})
    class Foo:
        @action_configuration({_MARKER: "method-value"})
        def perform_list(self):
            pass

    result = resolve_action_configuration(Foo(), "list_items")
    assert result == {_MARKER: "method-value", other: "class-other"}


def test_resolve_checks_action_method_name_for_hand_written_endpoints():
    """A custom __router endpoint (no perform_* counterpart) is found by its own name."""

    @action_configuration({_MARKER: "class-value"})
    class Foo:
        @action_configuration({_MARKER: "method-value"})
        def mine(self):
            pass

    assert resolve_action_configuration(Foo(), "mine") == {_MARKER: "method-value"}


def test_resolve_perform_method_wins_over_action_method_if_both_decorated():
    class Foo:
        @action_configuration({_MARKER: "action-method-value"})
        def update(self):
            pass

        @action_configuration({_MARKER: "perform-method-value"})
        def perform_update(self):
            pass

    assert resolve_action_configuration(Foo(), "update") == {_MARKER: "perform-method-value"}


# ---------------------------------------------------------------------------
# ByAction integration with resolve_action_configuration (resolution stays deferred)
# ---------------------------------------------------------------------------

def test_resolved_dict_keeps_byaction_values_unresolved():
    """resolve_action_configuration returns the raw dict - ByAction resolution happens later, at
    read-time (Context.configuration_for), not here - see the staleness bug this avoids."""

    @action_configuration({_MARKER: ByAction(list_items="read", update="write", default="read")})
    class Foo:
        pass

    result = resolve_action_configuration(Foo(), "update")
    assert isinstance(result[_MARKER], ByAction)


# ---------------------------------------------------------------------------
# extra_middlewares_for
# ---------------------------------------------------------------------------

class _DummyMiddleware(Middleware):
    async def __call__(self, _request, _viewset, _context, call_next):
        return await call_next()


class _OtherMiddleware(Middleware):
    async def __call__(self, _request, _viewset, _context, call_next):
        return await call_next()


def test_instance_key_is_appended_as_extra_middleware():
    extra_instance = _OtherMiddleware()
    config = {_DummyMiddleware: "some-config", extra_instance: None}
    global_instance = _DummyMiddleware()

    extras = extra_middlewares_for(config, [global_instance])
    assert extras == [extra_instance]


def test_multiple_instance_keys_preserve_dict_order():
    a, b = _DummyMiddleware(), _OtherMiddleware()
    extras = extra_middlewares_for({a: None, b: None}, [])
    assert extras == [a, b]


def test_class_key_matching_a_global_instance_is_not_treated_as_extra():
    global_instance = _DummyMiddleware()
    extras = extra_middlewares_for({_DummyMiddleware: "config"}, [global_instance])
    assert extras == []


def test_class_key_with_no_matching_global_instance_raises():
    with pytest.raises(ValueError, match="_DummyMiddleware"):
        extra_middlewares_for({_DummyMiddleware: "config"}, [])


def test_non_middleware_identifiers_are_ignored():
    """Plain (non-Middleware) identifiers - e.g. a context processor callable - are left alone;
    only Middleware classes/instances are inspected."""

    async def some_processor(_request, _viewset):
        return {}

    extras = extra_middlewares_for({some_processor: "config"}, [])
    assert extras == []


# ---------------------------------------------------------------------------
# End-to-end: full route_viewset pipeline exercising class+method merge, ByAction resolution via a
# context processor, and an ad-hoc injected middleware - all at once.
# ---------------------------------------------------------------------------

class Item(BaseModel):
    id: int
    name: str


async def authz_context_processor(_request, _viewset, config) -> dict:
    return {"authz": config}


class _TrackingMiddleware(Middleware):
    def __init__(self):
        self.calls = []

    async def __call__(self, _request, viewset, _context, call_next):
        self.calls.append(viewset.__class__.__name__)
        return await call_next()


def test_full_pipeline_merges_class_and_method_config_and_injects_extra_middleware():
    tracking = _TrackingMiddleware()
    settings.viewsets_context_processors = [authz_context_processor]

    router = APIRouter()

    @route_viewset(router, base_path="/items")
    @action_configuration({
        authz_context_processor: ByAction(list_items="read", update="write"), tracking: None,
    })
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, context: Context) -> list[Item]:
            return [Item(id=1, name=await context.authz)]

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/items")
    assert response.status_code == 200
    assert response.json() == [{"id": 1, "name": "read"}]
    assert tracking.calls == ["ItemViewSet"]


def test_method_level_config_overrides_class_level_in_full_pipeline():
    settings.viewsets_context_processors = [authz_context_processor]

    router = APIRouter()

    @route_viewset(router, base_path="/items")
    @action_configuration({authz_context_processor: "class-level"})
    class ItemViewSet(ListMixin[Item]):
        @action_configuration({authz_context_processor: "method-level"})
        async def perform_list(self, context: Context) -> list[Item]:
            return [Item(id=1, name=await context.authz)]

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/items")
    assert response.json() == [{"id": 1, "name": "method-level"}]
