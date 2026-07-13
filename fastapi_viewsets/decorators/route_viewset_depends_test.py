"""
End-to-end tests for the Middleware.depends() -> FastAPI Depends() bridge (see route_viewset.py's
_make_middleware_depends). Uses a generic dummy Middleware (not Session/Authorization/RateLimiter)
to keep these tests focused on the bridging mechanism itself, decoupled from any specific built-in
middleware's own behavior (those have their own end-to-end coverage already).
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


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    settings.viewsets_command_middleware = []
    settings.viewsets_context_processors = []


class Item(BaseModel):
    id: int
    name: str


class _RejectingMiddleware(Middleware):
    """Rejects every request in depends() - the onion __call__ never gets reached in practice
    (depends() already raised, aborting the request before lifecycle_runner ever starts), so
    __call__ - like Session/Authorization/RateLimiter's own - is a trivial passthrough."""

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
    assert response.json() == {"detail": "Rejected early"}


def test_depends_rejection_precedes_body_validation_422():
    """A malformed body AND a rejecting middleware on the same request - 401, not 422, since
    FastAPI resolves route-level dependencies() before parsing the request body."""
    settings.viewsets_command_middleware = [_RejectingMiddleware()]
    client = TestClient(_make_create_app())

    # "name" should be a string - this body would fail Item validation (422) if it were ever parsed
    response = client.post("/items", json={"id": "not-an-int", "name": 123})
    assert response.status_code == 401
    assert response.json() == {"detail": "Rejected early"}


def test_dependency_overrides_can_bypass_the_middleware_bridge():
    """app.dependency_overrides works on the bridged dependency - proves the new testability
    benefit (impossible with the old, purely-internal onion-chain-only mechanism). Overriding it to
    a no-op lets the (otherwise rejecting) route through, all the way to perform_*."""
    settings.viewsets_command_middleware = [_RejectingMiddleware()]

    router = APIRouter()

    @route_viewset(router, base_path="/items")
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context: Context) -> list[Item]:
            return [Item(id=1, name="Widget")]

    app = FastAPI()
    app.include_router(router)

    route = next(r for r in app.routes if r.path == "/items" and "GET" in r.methods)
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
