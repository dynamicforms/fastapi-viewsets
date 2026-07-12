import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_viewsets.conf import settings
from fastapi_viewsets.context import Context
from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.middleware import Middleware, ViewSetResult
from fastapi_viewsets.middleware.auth import Session


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    settings.viewsets_context_processors = []
    settings.viewsets_command_middleware = []
    settings.viewsets_auth_processors = []


def test_session_is_a_middleware():
    assert isinstance(Session(), Middleware)


@pytest.mark.asyncio
async def test_short_circuits_with_401_when_user_is_none():
    async def final_handler():
        raise AssertionError("call_next must not run when the session is missing/expired")

    context = Context({"user": None})
    result = await Session()(None, object(), context, final_handler)

    assert result.status_code == 401
    assert result.body == {"detail": "Session expired or invalid"}


@pytest.mark.asyncio
async def test_passes_through_when_user_resolves():
    async def final_handler():
        return ViewSetResult(body="ok")

    context = Context({"user": {"id": 1}})
    result = await Session()(None, object(), context, final_handler)

    assert result.body == "ok"
    assert result.status_code is None


@pytest.mark.asyncio
async def test_requires_auth_false_opts_out():
    async def final_handler():
        return ViewSetResult(body="public")

    class _OptOutViewset:
        requires_auth = False

    context = Context({"user": None})
    result = await Session()(None, _OptOutViewset(), context, final_handler)

    assert result.body == "public"
    assert result.status_code is None


@pytest.mark.asyncio
async def test_default_requires_auth_is_true_when_attribute_absent():
    async def final_handler():
        raise AssertionError("call_next must not run - viewset has no requires_auth override")

    context = Context({"user": None})
    result = await Session()(None, object(), context, final_handler)

    assert result.status_code == 401


# ---------------------------------------------------------------------------
# End-to-end: auth_context_processor + Session middleware wired into a real route
# ---------------------------------------------------------------------------

class Item(BaseModel):
    id: int
    name: str


def _make_app():
    from fastapi_viewsets.context.auth import auth_context_processor
    from fastapi_viewsets.context.auth.static import StaticUserAuthBackend
    from fastapi_viewsets.mixins import ListMixin

    settings.viewsets_context_processors = [auth_context_processor]
    settings.viewsets_command_middleware = [Session()]
    settings.viewsets_auth_processors = [StaticUserAuthBackend({"tok-jure": {"id": 1, "username": "jure"}})]

    router = APIRouter()

    @route_viewset(router, base_path="/items")
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, context: Context) -> list[Item]:
            user = await context.user
            return [Item(id=1, name=user["username"])]

    app = FastAPI()
    app.include_router(router)
    return app


def test_valid_session_token_reaches_endpoint():
    client = TestClient(_make_app())
    response = client.get("/items", headers={"x-session-token": "tok-jure"})
    assert response.status_code == 200
    assert response.json() == [{"id": 1, "name": "jure"}]


def test_missing_session_token_gets_401():
    client = TestClient(_make_app())
    response = client.get("/items")
    assert response.status_code == 401
    assert response.json() == {"detail": "Session expired or invalid"}


def test_invalid_session_token_gets_401():
    client = TestClient(_make_app())
    response = client.get("/items", headers={"x-session-token": "tok-unknown"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Session expired or invalid"}
