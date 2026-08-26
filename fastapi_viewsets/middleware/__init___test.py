import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_viewsets.conf import settings
from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.middleware import Middleware, run_command_chain, ViewSetResult


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


async def _unauthorized_middleware(_request, _viewset, _context, _call_next):
    """A middleware that short-circuits without calling call_next() - the same shape a real
    session-expiry check uses (see middleware/auth/__init__.py's Session)."""
    return ViewSetResult(body={"detail": "nope"}, status_code=401)


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
