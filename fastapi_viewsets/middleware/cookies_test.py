import json
import re

from datetime import datetime, timezone

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.responses import Response

from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.middleware import (
    apply_cookies,
    Cookie,
    DeleteCookie,
    ViewSetResult,
)

# ---------------------------------------------------------------------------
# Cookies with options: Cookie, DeleteCookie, set_cookie(), delete_cookie()
# ---------------------------------------------------------------------------


def _starlette_set_cookie(*args, **kwargs) -> list[str]:
    """The Set-Cookie headers Starlette's own Response.set_cookie() produces for these arguments."""
    response = Response()
    response.set_cookie(*args, **kwargs)
    return response.headers.getlist("set-cookie")


def _starlette_delete_cookie(*args, **kwargs) -> list[str]:
    response = Response()
    response.delete_cookie(*args, **kwargs)
    return response.headers.getlist("set-cookie")


SESSION_OPTIONS = {"max_age": 1209600, "path": "/", "secure": True, "httponly": True, "samesite": "lax"}


class Account(BaseModel):
    username: str


def _make_session_app() -> FastAPI:
    app = FastAPI()
    router = APIRouter()

    @route_viewset(router, base_path="/auth")
    class AuthViewSet:
        __router = APIRouter()

        @__router.post("login")
        async def login(self) -> ViewSetResult[Account]:
            result = ViewSetResult(body=Account(username="ana"))
            result.set_cookie("sessionid", "s3cr3t", **SESSION_OPTIONS)
            return result

        @__router.post("logout")
        async def logout(self) -> ViewSetResult[None]:
            result = ViewSetResult(body=None)
            result.delete_cookie("sessionid", path="/", secure=True, httponly=True, samesite="lax")
            return result

        @__router.post("mixed")
        async def mixed(self) -> ViewSetResult[None]:
            return ViewSetResult(
                body=None,
                cookies={
                    "plain": 42,
                    "scoped": Cookie("v", domain="example.com", path="/app", samesite="strict"),
                    "expiring": Cookie("v", expires=datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)),
                    "gone": DeleteCookie(path="/app"),
                },
            )

    app.include_router(router)
    return app


def test_set_cookie_and_delete_cookie_record_into_cookies():
    result = ViewSetResult(body=None, cookies={"plain": "x"})
    result.set_cookie("sessionid", "s3cr3t", **SESSION_OPTIONS)
    result.delete_cookie("old", path="/app", domain="example.com")
    assert result.cookies == {
        "plain": "x",
        "sessionid": Cookie("s3cr3t", max_age=1209600, secure=True, httponly=True),
        "old": DeleteCookie(path="/app", domain="example.com"),
    }
    result.delete_cookie("sessionid")
    assert result.cookies["sessionid"] == DeleteCookie()


def test_cookie_defaults_are_starlettes():
    response = Response()
    apply_cookies(response, {"a": Cookie("1"), "b": "2", "c": DeleteCookie()})
    assert response.headers.getlist("set-cookie") == [
        *_starlette_set_cookie("a", "1"),
        *_starlette_set_cookie("b", "2"),
        *_starlette_delete_cookie("c"),
    ]


def test_apply_cookies_passes_every_option_to_starlette():
    at = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    options = {
        "max_age": 60,
        "expires": at,
        "path": "/app",
        "domain": "example.com",
        "secure": True,
        "httponly": True,
        "samesite": "none",
    }
    deletion = {"path": "/app", "domain": "example.com", "secure": True, "httponly": True, "samesite": "strict"}
    response = Response()
    apply_cookies(response, {"a": Cookie("1", **options), "b": DeleteCookie(**deletion)})
    assert response.headers.getlist("set-cookie") == [
        *_starlette_set_cookie("a", "1", **options),
        *_starlette_delete_cookie("b", **deletion),
    ]


def test_apply_cookies_passes_partitioned_to_starlette():
    response = Response()
    try:
        expected = _starlette_set_cookie("a", "1", secure=True, samesite="none", partitioned=True)
    except ValueError as refusal:
        # the running Starlette/Python does not support the option, and refuses it the same way
        with pytest.raises(ValueError, match=re.escape(str(refusal))):
            apply_cookies(response, {"a": Cookie("1", secure=True, samesite="none", partitioned=True)})
        return
    apply_cookies(response, {"a": Cookie("1", secure=True, samesite="none", partitioned=True)})
    assert response.headers.getlist("set-cookie") == expected
    assert "Partitioned" in expected[0]


@pytest.mark.parametrize("expires", [None, 3600, "Wed, 02 Jan 2030 03:04:05 GMT", datetime(2030, 1, 2, 3, 4, 5)])
def test_cookies_survive_serialization_with_all_options(expires):
    original = ViewSetResult(
        body=None,
        cookies={
            "plain": 42,
            "session": Cookie(
                "s3cr3t",
                max_age=60,
                expires=expires,
                path="/app",
                domain="example.com",
                secure=True,
                httponly=True,
                samesite="strict",
                partitioned=True,
            ),
            "gone": DeleteCookie(path="/app", domain="example.com", secure=True, httponly=True, samesite=None),
        },
    )
    restored = ViewSetResult.__deserialize__(json.loads(json.dumps(original.__serialize__())))
    assert restored == original
    assert type(restored.cookies["session"].expires) is type(expires)


def test_an_action_sets_a_session_cookie_with_its_options_over_http():
    response = TestClient(_make_session_app()).post("/auth/login")
    assert response.status_code == 200
    assert response.json() == {"username": "ana"}
    assert response.headers.get_list("set-cookie") == _starlette_set_cookie("sessionid", "s3cr3t", **SESSION_OPTIONS)
    header = response.headers["set-cookie"]
    for attribute in ("HttpOnly", "Max-Age=1209600", "Path=/", "SameSite=lax", "Secure"):
        assert attribute in header


def test_an_action_deletes_a_cookie_over_http():
    response = TestClient(_make_session_app()).post("/auth/logout")
    assert response.status_code == 200
    assert response.headers.get_list("set-cookie") == _starlette_delete_cookie(
        "sessionid", path="/", secure=True, httponly=True, samesite="lax"
    )
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_plain_cookies_options_and_deletions_mix_in_one_result():
    response = TestClient(_make_session_app()).post("/auth/mixed")
    assert response.headers.get_list("set-cookie") == [
        *_starlette_set_cookie("plain", "42"),
        *_starlette_set_cookie("scoped", "v", domain="example.com", path="/app", samesite="strict"),
        *_starlette_set_cookie("expiring", "v", expires=datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)),
        *_starlette_delete_cookie("gone", path="/app"),
    ]


def test_cookies_set_through_viewset_result_keep_the_typed_response_schema():
    app = _make_session_app()
    schema = app.openapi()["paths"]["/auth/login"]["post"]["responses"]["200"]["content"]["application/json"]
    assert schema["schema"] == {"$ref": "#/components/schemas/Account"}


@pytest.mark.asyncio
async def test_an_action_sets_and_deletes_cookies_with_their_options_over_muxws():
    from muxws.frames import ABSENT

    from fastapi_viewsets.mux_ws.server import process_command
    from fastapi_viewsets.mux_ws.server_test import FakeStream

    app = _make_session_app()
    stream = FakeStream({":method": "POST", ":path": "/auth/login"})
    await process_command(ABSENT, stream, app=app)
    assert stream.status == 200
    assert stream.payload == {"username": "ana"}
    assert [stream.reply_headers["set-cookie"]] == _starlette_set_cookie("sessionid", "s3cr3t", **SESSION_OPTIONS)

    stream = FakeStream({":method": "POST", ":path": "/auth/logout"})
    await process_command(ABSENT, stream, app=app)
    assert [stream.reply_headers["set-cookie"]] == _starlette_delete_cookie(
        "sessionid", path="/", secure=True, httponly=True, samesite="lax"
    )
