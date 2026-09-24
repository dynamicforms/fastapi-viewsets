import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_viewsets.conf import settings
from fastapi_viewsets.context import SerializableObject
from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.middleware import (
    any_modifies_response_shape,
    Middleware,
    run_command_chain,
    unwrap_viewset_result_type,
    ViewSetResult,
)


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    settings.viewsets_command_middleware = []


# ---------------------------------------------------------------------------
# ViewSetResult / run_command_chain
# ---------------------------------------------------------------------------


def test_viewset_result_defaults():
    result = ViewSetResult(body="hello")
    assert result.body == "hello"
    assert result.headers == {}
    assert result.cookies == {}
    assert result.status_code is None


@pytest.mark.asyncio
async def test_run_command_chain_with_no_middleware_just_calls_final_handler():
    async def final_handler():
        return ViewSetResult(body="direct")

    result = await run_command_chain([], None, None, None, final_handler)
    assert result.body == "direct"


def test_any_modifies_response_shape_defaults_false_for_plain_functions_and_middleware():
    async def plain_fn(_r, _v, _c, call_next):
        return await call_next()

    class PlainMiddleware(Middleware):
        async def __call__(self, _request, _viewset, _context, call_next):
            return await call_next()

    assert any_modifies_response_shape([plain_fn, PlainMiddleware()]) is False


def test_any_modifies_response_shape_true_if_any_entry_declares_it():
    async def plain_fn(_r, _v, _c, call_next):
        return await call_next()

    async def reshaping_fn(_r, _v, _c, call_next):
        return await call_next()

    reshaping_fn.modifies_response_shape = True

    assert any_modifies_response_shape([plain_fn, reshaping_fn]) is True


def test_middleware_is_abstract():
    with pytest.raises(TypeError):
        Middleware()


@pytest.mark.asyncio
async def test_middleware_instance_is_a_drop_in_command_middleware():
    """A Middleware subclass instance is callable with the exact CommandMiddleware signature -
    run_command_chain doesn't need to know or care whether an entry is a function or a class."""
    calls = []

    class TrackingMiddleware(Middleware):
        async def __call__(self, request, viewset, context, call_next):
            calls.append((request, viewset, context))
            return await call_next()

    async def final_handler():
        return ViewSetResult(body="ok")

    sentinel_request, sentinel_viewset, sentinel_context = object(), object(), object()
    result = await run_command_chain(
        [TrackingMiddleware()], sentinel_request, sentinel_viewset, sentinel_context, final_handler
    )
    assert result.body == "ok"
    assert calls == [(sentinel_request, sentinel_viewset, sentinel_context)]


@pytest.mark.asyncio
async def test_run_command_chain_runs_middleware_in_order_around_execution():
    calls = []

    async def mw_a(_request, _viewset, _context, call_next):
        calls.append("a-before")
        result = await call_next()
        calls.append("a-after")
        return result

    async def mw_b(_request, _viewset, _context, call_next):
        calls.append("b-before")
        result = await call_next()
        calls.append("b-after")
        return result

    async def final_handler():
        calls.append("execute")
        return ViewSetResult(body="ok")

    result = await run_command_chain([mw_a, mw_b], None, None, None, final_handler)
    assert result.body == "ok"
    assert calls == ["a-before", "b-before", "execute", "b-after", "a-after"]


@pytest.mark.asyncio
async def test_run_command_chain_middleware_can_mutate_result():
    async def cookie_mw(_request, _viewset, _context, call_next):
        result = await call_next()
        result.cookies["sessionid"] = "s3cr3t"
        return result

    async def final_handler():
        return ViewSetResult(body={"ok": True})

    result = await run_command_chain([cookie_mw], None, None, None, final_handler)
    assert result.body == {"ok": True}
    assert result.cookies == {"sessionid": "s3cr3t"}


@pytest.mark.asyncio
async def test_run_command_chain_middleware_receives_request_viewset_context():
    received = {}

    async def mw(request, viewset, context, call_next):
        received["request"] = request
        received["viewset"] = viewset
        received["context"] = context
        return await call_next()

    sentinel_request, sentinel_viewset, sentinel_context = object(), object(), object()

    async def final_handler():
        return ViewSetResult(body=None)

    await run_command_chain([mw], sentinel_request, sentinel_viewset, sentinel_context, final_handler)
    assert received == {"request": sentinel_request, "viewset": sentinel_viewset, "context": sentinel_context}


# ---------------------------------------------------------------------------
# route_viewset integration - command middleware sets a real cookie (finalize_response
# replacement) and reshapes the body, exactly like the old finalize_response hook did.
# ---------------------------------------------------------------------------


class LoginResult(BaseModel):
    is_authenticated: bool
    session_key: str | None = None


async def _session_cookie_middleware(_request, _viewset, _context, call_next):
    result = await call_next()
    if isinstance(result.body, LoginResult):
        data = result.body.model_dump()
        session_key = data.pop("session_key", None)
        if session_key is not None:
            result.cookies["sessionid"] = session_key
        result.body = data
    return result


_session_cookie_middleware.modifies_response_shape = True  # strips session_key - see test below


def _make_login_app():
    app = FastAPI()
    router = APIRouter()

    @route_viewset(router, base_path="/session")
    class SessionViewSet:
        __router = APIRouter()

        @__router.post("login")
        async def login(self) -> LoginResult:
            return LoginResult(is_authenticated=True, session_key="s3cr3t")

    app.include_router(router)
    return app


def test_command_middleware_sets_cookie_and_strips_field():
    settings.viewsets_command_middleware = [_session_cookie_middleware]
    client = TestClient(_make_login_app())
    response = client.post("/session/login")

    assert response.status_code == 200
    assert response.cookies.get("sessionid") == "s3cr3t"
    assert response.json() == {"is_authenticated": True}


def test_no_command_middleware_configured_is_unaffected():
    """Default (no middleware registered) behaviour is unchanged - no cookie, body untouched."""
    client = TestClient(_make_login_app())
    response = client.post("/session/login")

    assert response.status_code == 200
    assert "sessionid" not in response.cookies
    assert response.json() == {"is_authenticated": True, "session_key": "s3cr3t"}


def _login_response_schema(app: FastAPI) -> dict:
    return app.openapi()["paths"]["/session/login"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]


def test_no_command_middleware_configured_keeps_typed_response_schema():
    app = _make_login_app()
    assert _login_response_schema(app) == {"$ref": "#/components/schemas/LoginResult"}


def test_command_middleware_not_declaring_modifies_response_shape_keeps_typed_response_schema():
    """A middleware that only attaches a cookie (the common case) doesn't opt into
    modifies_response_shape - the endpoint's declared return type stays trusted, both for the
    OpenAPI docs and for FastAPI's own response validation."""

    async def cookie_only_middleware(_request, _viewset, _context, call_next):
        result = await call_next()
        result.cookies["sessionid"] = "s3cr3t"
        return result

    settings.viewsets_command_middleware = [cookie_only_middleware]
    app = _make_login_app()

    assert _login_response_schema(app) == {"$ref": "#/components/schemas/LoginResult"}
    response = TestClient(app).post("/session/login")
    assert response.cookies.get("sessionid") == "s3cr3t"
    assert response.json() == {"is_authenticated": True, "session_key": "s3cr3t"}


def test_command_middleware_declaring_modifies_response_shape_untypes_response_schema():
    """A middleware that opts into modifies_response_shape=True loses the typed OpenAPI schema for
    every route - the tradeoff documented for modifies_response_shape."""
    settings.viewsets_command_middleware = [_session_cookie_middleware]
    app = _make_login_app()

    assert _login_response_schema(app) == {}


async def _unauthorized_middleware(_request, _viewset, _context, _call_next):
    """A middleware that short-circuits without calling call_next() - the same shape a real
    session-expiry check uses (see middleware/auth/__init__.py's Session)."""
    return ViewSetResult(body={"detail": "nope"}, status_code=401)


_unauthorized_middleware.modifies_response_shape = True  # short-circuit body doesn't match LoginResult


def test_command_middleware_status_code_is_applied_to_the_real_response():
    settings.viewsets_command_middleware = [_unauthorized_middleware]
    client = TestClient(_make_login_app())
    response = client.post("/session/login")

    assert response.status_code == 401
    assert response.json() == {"detail": "nope"}


def test_command_middleware_default_status_code_leaves_response_untouched():
    async def passthrough_middleware(_request, _viewset, _context, call_next):
        return await call_next()

    settings.viewsets_command_middleware = [passthrough_middleware]
    client = TestClient(_make_login_app())
    response = client.post("/session/login")

    assert response.status_code == 200


def test_command_middleware_does_not_run_in_celery_worker_path():
    """Command middleware only runs in the outer, HTTP-facing route_viewset call - never in
    celery_viewset_server's inner, worker-only lifecycle_runner call (no live Response there),
    same as finalize_response worked before it."""
    from unittest.mock import MagicMock

    from fastapi_viewsets.decorators.celery_viewset import celery_viewset_server
    from fastapi_viewsets.mixins import ListMixin

    calls = []

    async def tracking_middleware(_request, _viewset, _context, call_next):
        calls.append("middleware-ran")
        return await call_next()

    settings.viewsets_command_middleware = [tracking_middleware]

    celery_app = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task

    @celery_viewset_server(celery_app=celery_app, task_prefix="items")
    class ItemViewSet(ListMixin[int]):
        async def perform_list(self, _context) -> list[int]:
            return [1]

    result = registered_tasks["items.list_items"](context={})
    assert result == [1]
    assert calls == []  # middleware never ran - no live Response in the worker path


# ---------------------------------------------------------------------------
# ViewSetResult returned directly by an endpoint - see docs/guide/command-middleware.md
# ---------------------------------------------------------------------------


def test_viewset_result_is_a_serializable_object():
    assert isinstance(ViewSetResult(body=None), SerializableObject)


def test_viewset_result_serialize_deserialize_roundtrip_with_plain_body():
    original = ViewSetResult(body={"ok": True}, headers={"Location": "/x"}, cookies={"a": "b"}, status_code=302)
    restored = ViewSetResult.__deserialize__(original.__serialize__())
    assert restored == original


def test_viewset_result_serialize_converts_pydantic_body():
    class Item(BaseModel):
        id: int
        name: str

    data = ViewSetResult(body=Item(id=1, name="widget")).__serialize__()
    assert data["body"] == {"id": 1, "name": "widget"}


def test_unwrap_viewset_result_type_unwraps_generic():
    class Item(BaseModel):
        id: int

    assert unwrap_viewset_result_type(ViewSetResult[Item]) is Item


def test_unwrap_viewset_result_type_passes_through_non_viewset_result():
    class Item(BaseModel):
        id: int

    assert unwrap_viewset_result_type(Item) is Item
    assert unwrap_viewset_result_type(None) is None


def _make_redirect_app():
    app = FastAPI()
    router = APIRouter()

    @route_viewset(router, base_path="/redirect")
    class RedirectViewSet:
        __router = APIRouter()

        @__router.post("go")
        async def go(self) -> ViewSetResult[None]:
            return ViewSetResult(body=None, status_code=302, headers={"Location": "/target"})

    app.include_router(router)
    return app


def test_endpoint_returning_viewset_result_directly_sets_status_and_headers():
    """An endpoint (not a command middleware) can reach status_code/headers itself by returning a
    ViewSetResult - final_handler passes it through instead of wrapping it a second time."""
    client = TestClient(_make_redirect_app(), follow_redirects=False)
    response = client.post("/redirect/go")

    assert response.status_code == 302
    assert response.headers["location"] == "/target"


def test_endpoint_returning_viewset_result_directly_still_runs_command_middleware():
    """Global command middleware still gets a chance at the ViewSetResult an endpoint returned
    itself - nothing about final_handler's pass-through bypasses the chain."""

    async def cookie_middleware(_request, _viewset, _context, call_next):
        result = await call_next()
        result.cookies["tracking"] = "1"
        return result

    settings.viewsets_command_middleware = [cookie_middleware]
    client = TestClient(_make_redirect_app(), follow_redirects=False)
    response = client.post("/redirect/go")

    assert response.status_code == 302
    assert response.cookies.get("tracking") == "1"


def test_declared_return_type_viewset_result_of_x_documents_as_x():
    """-> ViewSetResult[X] documents/validates identically to a plain -> X - see
    unwrap_viewset_result_type."""
    app = FastAPI()
    router = APIRouter()

    class Item(BaseModel):
        id: int
        name: str

    @route_viewset(router, base_path="/items")
    class ItemViewSet:
        __router = APIRouter()

        @__router.post("")
        async def make_item(self) -> ViewSetResult[Item]:
            return ViewSetResult(body=Item(id=1, name="widget"), status_code=201)

    app.include_router(router)

    client = TestClient(app)
    response = client.post("/items")
    assert response.status_code == 201
    assert response.json() == {"id": 1, "name": "widget"}

    schema = app.openapi()["paths"]["/items"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/Item"}
