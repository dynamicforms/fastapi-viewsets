import json

import pytest

from fastapi import Request

from fastapi_viewsets.context.auth.static import (
    _StaticUserLazy,
    StaticUserAuthBackend,
    StaticUserCookieAuthBackend,
)


def _make_request(headers: dict[str, str]) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw_headers, "method": "GET", "path": "/"})


def _make_cookie_request(cookies: dict[str, str]) -> Request:
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = {"cookie": cookie_header} if cookies else {}
    return _make_request(headers)


def test_try_handle_returns_lazy_for_known_token():
    backend = StaticUserAuthBackend({"tok-jure": {"id": 1, "username": "jure"}})
    lazy = backend.try_handle(_make_request({"x-session-token": "tok-jure"}))
    assert isinstance(lazy, _StaticUserLazy)


def test_try_handle_returns_none_for_unknown_token():
    backend = StaticUserAuthBackend({"tok-jure": {"id": 1, "username": "jure"}})
    assert backend.try_handle(_make_request({"x-session-token": "tok-unknown"})) is None


def test_try_handle_returns_none_when_header_missing():
    backend = StaticUserAuthBackend({"tok-jure": {"id": 1, "username": "jure"}})
    assert backend.try_handle(_make_request({})) is None


def test_try_handle_uses_configurable_header_name():
    backend = StaticUserAuthBackend({"tok-jure": {"id": 1}}, header_name="x-api-key")
    assert backend.try_handle(_make_request({"x-session-token": "tok-jure"})) is None
    lazy = backend.try_handle(_make_request({"x-api-key": "tok-jure"}))
    assert isinstance(lazy, _StaticUserLazy)


@pytest.mark.asyncio
async def test_lazy_resolves_synchronously_to_user_data():
    backend = StaticUserAuthBackend({"tok-jure": {"id": 1, "username": "jure"}})
    lazy = backend.try_handle(_make_request({"x-session-token": "tok-jure"}))
    assert await lazy == {"id": 1, "username": "jure"}
    assert lazy.is_resolved is True


def test_from_json_file_loads_token_mapping(tmp_path):
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"tok-jure": {"id": 1, "username": "jure"}}))

    backend = StaticUserAuthBackend.from_json_file(path)
    assert backend.users_by_token == {"tok-jure": {"id": 1, "username": "jure"}}


@pytest.mark.asyncio
async def test_serialize_recipe_is_already_json_safe_and_round_trips():
    lazy = _StaticUserLazy({"id": 1, "username": "jure"})
    recipe = lazy.serialize_recipe()
    assert recipe == {"id": 1, "username": "jure"}

    restored = _StaticUserLazy.deserialize_recipe(recipe)
    assert await restored == {"id": 1, "username": "jure"}


# ---------------------------------------------------------------------------
# StaticUserCookieAuthBackend
# ---------------------------------------------------------------------------

def test_cookie_backend_returns_lazy_for_known_token():
    backend = StaticUserCookieAuthBackend({"tok-jure": {"id": 1, "username": "jure"}})
    lazy = backend.try_handle(_make_cookie_request({"sessionid": "tok-jure"}))
    assert isinstance(lazy, _StaticUserLazy)


def test_cookie_backend_returns_none_for_unknown_token():
    backend = StaticUserCookieAuthBackend({"tok-jure": {"id": 1, "username": "jure"}})
    assert backend.try_handle(_make_cookie_request({"sessionid": "tok-unknown"})) is None


def test_cookie_backend_returns_none_when_cookie_missing():
    backend = StaticUserCookieAuthBackend({"tok-jure": {"id": 1, "username": "jure"}})
    assert backend.try_handle(_make_cookie_request({})) is None


def test_cookie_backend_ignores_the_header():
    """A cookie-only backend never looks at the X-Session-Token header."""
    backend = StaticUserCookieAuthBackend({"tok-jure": {"id": 1, "username": "jure"}})
    assert backend.try_handle(_make_request({"x-session-token": "tok-jure"})) is None


def test_cookie_backend_uses_configurable_cookie_name():
    backend = StaticUserCookieAuthBackend({"tok-jure": {"id": 1}}, cookie_name="my_session")
    assert backend.try_handle(_make_cookie_request({"sessionid": "tok-jure"})) is None
    lazy = backend.try_handle(_make_cookie_request({"my_session": "tok-jure"}))
    assert isinstance(lazy, _StaticUserLazy)


def test_cookie_backend_from_json_file_loads_token_mapping(tmp_path):
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"tok-jure": {"id": 1, "username": "jure"}}))

    backend = StaticUserCookieAuthBackend.from_json_file(path)
    assert backend.users_by_token == {"tok-jure": {"id": 1, "username": "jure"}}


@pytest.mark.asyncio
async def test_header_and_cookie_backends_compose_via_the_auth_processor_chain():
    """The documented pattern: register both, first match wins - a request carrying either
    credential shape is recognized."""
    from fastapi_viewsets.conf import settings
    from fastapi_viewsets.context.auth import auth_context_processor

    users = {"tok-jure": {"id": 1, "username": "jure"}}
    settings.viewsets_auth_processors = [StaticUserCookieAuthBackend(users), StaticUserAuthBackend(users)]
    try:
        cookie_result = await auth_context_processor(_make_cookie_request({"sessionid": "tok-jure"}), None)
        assert await cookie_result["user"] == {"id": 1, "username": "jure"}

        header_result = await auth_context_processor(_make_request({"x-session-token": "tok-jure"}), None)
        assert await header_result["user"] == {"id": 1, "username": "jure"}
    finally:
        settings.viewsets_auth_processors = []
