"""
End-to-end tests of the Middleware.depends() -> FastAPI Depends() bridge (`_make_middleware_depends`),
with a dummy Middleware rather than a built-in one.
"""

import pytest

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_viewsets.conf import settings
from fastapi_viewsets.context import Context
from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.middleware import Middleware, ViewSetResult
from fastapi_viewsets.mixins import CreateMixin, ListMixin

from .conftest import iter_routes


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    settings.viewsets_command_middleware = []
    settings.viewsets_context_processors = []


class Item(BaseModel):
    id: int
    name: str


class _RejectingMiddleware(Middleware):
    """Rejects every request in depends(), so its passthrough __call__ is never reached."""

    async def depends(self, _request, _cls, _context) -> None:
        raise HTTPException(status_code=401, detail="Rejected early")

    async def __call__(self, _request, _viewset, _context, call_next) -> ViewSetResult:
        return await call_next()


def _make_list_only_app() -> FastAPI:
    router = APIRouter()

    @route_viewset(router, base_path="/items")
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context: Context) -> list[Item]:
            raise AssertionError("perform_* must never run - depends() already rejected")

    app = FastAPI()
    app.include_router(router)
    return app


def _make_create_app() -> FastAPI:
    router = APIRouter()

    @route_viewset(router, base_path="/items")
    class ItemViewSet(CreateMixin[int, Item]):
        async def perform_create(self, _context: Context, _data: Item) -> Item:
            raise AssertionError("perform_* must never run - depends() already rejected")

    app = FastAPI()
    app.include_router(router)
    return app


def test_depends_rejects_before_perform_runs():
    settings.viewsets_command_middleware = [_RejectingMiddleware()]
    client = TestClient(_make_list_only_app())

    response = client.get("/items")
    assert response.status_code == 401
    assert response.json() == {
        "detail": "Rejected early",
        "detail_code": "fastapi-viewsets.http_error",
        "detail_params": {"status": 401},
        "field_errors": {},
    }


def test_depends_rejection_precedes_body_validation_422():
    """A malformed body and a rejecting middleware: 401, not 422, as route dependencies run before body parsing."""
    settings.viewsets_command_middleware = [_RejectingMiddleware()]
    client = TestClient(_make_create_app())

    # "name" should be a string - this body would fail Item validation (422) if it were ever parsed
    response = client.post("/items", json={"id": "not-an-int", "name": 123})
    assert response.status_code == 401
    assert response.json() == {
        "detail": "Rejected early",
        "detail_code": "fastapi-viewsets.http_error",
        "detail_params": {"status": 401},
        "field_errors": {},
    }


def test_dependency_overrides_can_bypass_the_middleware_bridge():
    """Overriding the bridged dependency with a no-op in app.dependency_overrides lets the request through."""
    settings.viewsets_command_middleware = [_RejectingMiddleware()]

    router = APIRouter()

    @route_viewset(router, base_path="/items")
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context: Context) -> list[Item]:
            return [Item(id=1, name="Widget")]

    app = FastAPI()
    app.include_router(router)

    route = next(r for r in iter_routes(app.routes) if r.path == "/items" and "GET" in r.methods)
    bridge_dependency = route.dependencies[-1].dependency

    async def _allow(_request=None) -> None:
        return None

    app.dependency_overrides[bridge_dependency] = _allow

    client = TestClient(app)
    response = client.get("/items")
    assert response.status_code == 200
    assert response.json() == [{"id": 1, "name": "Widget"}]


# ---------------------------------------------------------------------------
# Context is built at most once per request, shared between depends() and perform_*
# ---------------------------------------------------------------------------

_resolution_calls: list[str] = []


async def _counting_context_processor(_request, _viewset) -> dict:
    _resolution_calls.append("resolved")
    return {"marker": "value"}


class _ContextReadingMiddleware(Middleware):
    async def depends(self, _request, _cls, context) -> None:
        await context.marker  # forces context to be built (and cached) this early

    async def __call__(self, _request, _viewset, _context, call_next) -> ViewSetResult:
        return await call_next()


def test_context_is_built_only_once_per_request():
    _resolution_calls.clear()
    settings.viewsets_context_processors = [_counting_context_processor]
    settings.viewsets_command_middleware = [_ContextReadingMiddleware()]

    router = APIRouter()

    @route_viewset(router, base_path="/items")
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, context: Context) -> list[Item]:
            marker = await context.marker
            return [Item(id=1, name=marker)]

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/items")
    assert response.status_code == 200
    assert response.json() == [{"id": 1, "name": "value"}]
    assert _resolution_calls == ["resolved"]  # not ["resolved", "resolved"]
