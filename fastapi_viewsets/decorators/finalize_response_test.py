"""
Tests for the optional `finalize_response` hook on `route_viewset` (see
docs/guide/routers.md "Response-level side effects: finalize_response").

Motivating case: an action executed via `celery_viewset` (a Celery worker, no live Response
object) needs to set a cookie based on part of its (JSON-serializable) return value once the
result reaches the FastAPI process. `finalize_response(self, response, result)` is called with
a REAL `Response` for the current connection and the resolved result, and may mutate the
response (e.g. `response.set_cookie(...)`) and/or return a modified result (e.g. with an
internal-only field stripped before it becomes the JSON body).
"""

from fastapi import APIRouter, FastAPI, Response
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_viewsets.decorators import route_viewset


class LoginResult(BaseModel):
    is_authenticated: bool
    session_key: str | None = None


def make_app():
    app = FastAPI()
    router = APIRouter()

    @route_viewset(router, base_path="/session")
    class SessionViewSet:
        __router = APIRouter()

        @__router.post("login")
        async def login(self) -> LoginResult:
            return LoginResult(is_authenticated=True, session_key="s3cr3t")

        async def finalize_response(self, response: Response, result: LoginResult) -> dict:
            data = result.model_dump()
            session_key = data.pop("session_key", None)
            if session_key is not None:
                response.set_cookie("sessionid", session_key)
            return data

    app.include_router(router)
    return app


def make_app_without_hook():
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


def test_finalize_response_sets_cookie_and_strips_field():
    client = TestClient(make_app())
    response = client.post("/session/login")

    assert response.status_code == 200
    assert response.cookies.get("sessionid") == "s3cr3t"
    assert response.json() == {"is_authenticated": True}


def test_viewset_without_finalize_response_is_unaffected():
    """Backward compatibility: viewsets that don't define finalize_response behave exactly as
    before - no extra `response` param is injected, no cookie is set."""
    client = TestClient(make_app_without_hook())
    response = client.post("/session/login")

    assert response.status_code == 200
    assert "sessionid" not in response.cookies
    assert response.json() == {"is_authenticated": True, "session_key": "s3cr3t"}
