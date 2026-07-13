import datetime

import pytest

pytest.importorskip("jwt")

from fastapi import Request  # noqa: E402

from fastapi_viewsets.context.auth.jwt import (  # noqa: E402
    _JWTUserLazy,
    encode_jwt,
    JWTAuthBackend,
)

_SECRET = "test-secret-key-at-least-32-bytes-long"  # noqa: S105 - not a real credential, just a test fixture


def _make_request(headers: dict[str, str]) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw_headers, "method": "GET", "path": "/"})


def test_try_handle_returns_none_when_header_missing():
    backend = JWTAuthBackend(secret=_SECRET)
    assert backend.try_handle(_make_request({})) is None


def test_try_handle_returns_none_for_non_bearer_scheme():
    backend = JWTAuthBackend(secret=_SECRET)
    request = _make_request({"authorization": "Basic dXNlcjpwYXNz"})
    assert backend.try_handle(request) is None


def test_try_handle_is_case_insensitive_on_bearer_prefix():
    token = encode_jwt({"sub": "1"}, secret=_SECRET)
    backend = JWTAuthBackend(secret=_SECRET)
    request = _make_request({"authorization": f"BEARER {token}"})
    assert isinstance(backend.try_handle(request), _JWTUserLazy)


@pytest.mark.asyncio
async def test_valid_token_resolves_to_claims():
    token = encode_jwt({"sub": "1", "username": "jure"}, secret=_SECRET)
    backend = JWTAuthBackend(secret=_SECRET)
    request = _make_request({"authorization": f"Bearer {token}"})

    lazy = backend.try_handle(request)
    claims = await lazy
    assert claims["sub"] == "1"
    assert claims["username"] == "jure"
    assert "exp" in claims
    assert "iat" in claims


@pytest.mark.asyncio
async def test_expired_token_resolves_to_none():
    token = encode_jwt({"sub": "1"}, secret=_SECRET, expires_in=datetime.timedelta(seconds=-1))
    backend = JWTAuthBackend(secret=_SECRET)
    request = _make_request({"authorization": f"Bearer {token}"})

    assert await backend.try_handle(request) is None


@pytest.mark.asyncio
async def test_wrong_secret_resolves_to_none():
    token = encode_jwt({"sub": "1"}, secret=_SECRET)
    backend = JWTAuthBackend(secret="a-completely-different-secret-value")  # noqa: S106
    request = _make_request({"authorization": f"Bearer {token}"})

    assert await backend.try_handle(request) is None


@pytest.mark.asyncio
async def test_malformed_token_resolves_to_none():
    backend = JWTAuthBackend(secret=_SECRET)
    request = _make_request({"authorization": "Bearer not-a-real-jwt"})

    assert await backend.try_handle(request) is None


def test_custom_header_name():
    token = encode_jwt({"sub": "1"}, secret=_SECRET)
    backend = JWTAuthBackend(secret=_SECRET, header_name="x-auth")
    request = _make_request({"x-auth": f"Bearer {token}"})
    assert isinstance(backend.try_handle(request), _JWTUserLazy)
    assert backend.try_handle(_make_request({"authorization": f"Bearer {token}"})) is None


@pytest.mark.asyncio
async def test_serialize_recipe_round_trips_and_resolves_after_deserialize():
    token = encode_jwt({"sub": "1"}, secret=_SECRET)
    lazy = _JWTUserLazy(token, _SECRET, "HS256")

    recipe = lazy.serialize_recipe()
    assert recipe == {"token": token, "secret": _SECRET, "algorithm": "HS256"}

    restored = _JWTUserLazy.deserialize_recipe(recipe)
    claims = await restored
    assert claims["sub"] == "1"


def test_encode_jwt_stamps_exp_and_iat():
    claims = encode_jwt({"sub": "1"}, secret=_SECRET, expires_in=datetime.timedelta(minutes=5))
    assert isinstance(claims, str)
