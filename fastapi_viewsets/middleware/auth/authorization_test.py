import pytest

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_viewsets.action_configuration import action_configuration
from fastapi_viewsets.conf import settings
from fastapi_viewsets.context import Context
from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.middleware import ViewSetResult
from fastapi_viewsets.middleware.auth.authorization import Authorization
from fastapi_viewsets.mixins import ListMixin, RetrieveMixin


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    settings.viewsets_context_processors = []
    settings.viewsets_command_middleware = []


async def _final_ok():
    return ViewSetResult(body="ok")


@pytest.mark.asyncio
async def test_unconfigured_passes_through_and_context_authorization_is_none():
    context = Context({})
    result = await Authorization()(None, object(), context, _final_ok)

    assert result.body == "ok"
    assert await context.authorization is None


@pytest.mark.asyncio
async def test_non_callable_config_is_exposed_but_not_enforced():
    context = Context({}, action_configuration={Authorization: "owner-only"})
    result = await Authorization()(None, object(), context, _final_ok)

    assert result.body == "ok"
    assert await context.authorization == "owner-only"


@pytest.mark.asyncio
async def test_callable_config_returning_true_passes_through():
    context = Context({}, action_configuration={Authorization: lambda _r, _v, _c: True})
    result = await Authorization()(None, object(), context, _final_ok)

    assert result.body == "ok"


@pytest.mark.asyncio
async def test_callable_config_returning_false_rejects_with_403():
    async def final_handler():
        raise AssertionError("call_next must not run when the check fails")

    context = Context({}, action_configuration={Authorization: lambda _r, _v, _c: False})
    result = await Authorization()(None, object(), context, final_handler)

    assert result.status_code == 403
    assert result.body == {"detail": "Not authorized to perform this action"}


@pytest.mark.asyncio
async def test_async_callable_config_is_awaited():
    async def check(_request, _viewset, _context):
        return False

    async def final_handler():
        raise AssertionError("call_next must not run when the check fails")

    context = Context({}, action_configuration={Authorization: check})
    result = await Authorization()(None, object(), context, final_handler)

    assert result.status_code == 403


@pytest.mark.asyncio
async def test_callable_config_receives_request_viewset_context():
    received = {}

    def check(request, viewset, context):
        received["request"] = request
        received["viewset"] = viewset
        received["context"] = context
        return True

    sentinel_request, sentinel_viewset = object(), object()
    context = Context({}, action_configuration={Authorization: check})
    await Authorization()(sentinel_request, sentinel_viewset, context, _final_ok)

    assert received == {"request": sentinel_request, "viewset": sentinel_viewset, "context": context}


# ---------------------------------------------------------------------------
# End-to-end: middleware-level rejection (callable config) via a real route
# ---------------------------------------------------------------------------

class Item(BaseModel):
    id: int
    name: str
    owner: str


_DATABASE = {1: Item(id=1, name="Widget", owner="jure")}


def test_callable_config_rejects_a_real_route_with_403():
    settings.viewsets_command_middleware = [Authorization()]

    router = APIRouter()

    @route_viewset(router, base_path="/admin-items")
    @action_configuration({Authorization: lambda _request, _viewset, _context: False})
    class AdminItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context: Context) -> list[Item]:
            return list(_DATABASE.values())

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/admin-items")
    assert response.status_code == 403
    assert response.json() == {"detail": "Not authorized to perform this action"}


# ---------------------------------------------------------------------------
# End-to-end: object-level rejection - perform_* reads context.authorization itself
# ---------------------------------------------------------------------------

async def _current_user_processor(request, _viewset) -> dict:
    return {"user": request.headers.get("x-user")}


def test_perform_method_reads_authorization_and_rejects_itself():
    settings.viewsets_context_processors = [_current_user_processor]
    settings.viewsets_command_middleware = [Authorization()]

    router = APIRouter()

    @route_viewset(router, base_path="/items", pk_field_name="id")
    @action_configuration({Authorization: "owner-only"})
    class ItemViewSet(RetrieveMixin[int, Item]):
        async def perform_retrieve(self, context: Context, pk: int) -> Item:
            item = _DATABASE[pk]
            authz = await context.authorization
            user = await context.user
            if authz == "owner-only" and item.owner != user:
                raise HTTPException(status_code=403, detail="Not your item")
            return item

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    owner_response = client.get("/items/1", headers={"x-user": "jure"})
    assert owner_response.status_code == 200
    assert owner_response.json()["owner"] == "jure"

    other_response = client.get("/items/1", headers={"x-user": "someone-else"})
    assert other_response.status_code == 403
    assert other_response.json() == {"detail": "Not your item"}
