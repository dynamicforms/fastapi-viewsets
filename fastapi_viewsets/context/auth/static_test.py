import json

import pytest

from fastapi import Request

from fastapi_viewsets.context.auth.static import _StaticUserLazy, StaticUserAuthBackend


def _make_request(headers: dict[str, str]) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw_headers, "method": "GET", "path": "/"})


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
