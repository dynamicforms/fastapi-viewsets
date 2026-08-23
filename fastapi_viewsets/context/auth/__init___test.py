import pytest

from fastapi_viewsets.conf import settings
from fastapi_viewsets.context.auth import auth_context_processor, AuthBackend
from fastapi_viewsets.context.auth.static import StaticUserAuthBackend


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    settings.viewsets_auth_processors = []


def test_auth_backend_is_abstract():
    with pytest.raises(TypeError):
        AuthBackend()


@pytest.mark.asyncio
async def test_no_backends_configured_yields_none_user():
    settings.viewsets_auth_processors = []
    result = await auth_context_processor(None, None)
    assert result == {"user": None}


@pytest.mark.asyncio
async def test_first_matching_backend_wins():
    class _AlwaysMatch(AuthBackend):
        def __init__(self, tag):
            self.tag = tag

        def try_handle(self, _request):
            return self.tag

    settings.viewsets_auth_processors = [_AlwaysMatch("first"), _AlwaysMatch("second")]
    result = await auth_context_processor(None, None)
    assert result == {"user": "first"}


@pytest.mark.asyncio
async def test_non_matching_backend_falls_through_to_next():
    class _NoMatch(AuthBackend):
        def try_handle(self, _request):
            return None

    class _Match(AuthBackend):
        def try_handle(self, _request):
            return "matched"

    settings.viewsets_auth_processors = [_NoMatch(), _Match()]
    result = await auth_context_processor(None, None)
    assert result == {"user": "matched"}


@pytest.mark.asyncio
async def test_no_backend_matches_yields_none_user():
    class _NoMatch(AuthBackend):
        def try_handle(self, _request):
            return None

    settings.viewsets_auth_processors = [_NoMatch(), _NoMatch()]
    result = await auth_context_processor(None, None)
    assert result == {"user": None}


@pytest.mark.asyncio
async def test_static_backend_wired_in_via_auth_context_processor():
    from fastapi import Request

    backend = StaticUserAuthBackend({"tok-jure": {"id": 1, "username": "jure"}})
    settings.viewsets_auth_processors = [backend]

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-session-token", b"tok-jure")],
        }
    )
    result = await auth_context_processor(request, None)
    assert await result["user"] == {"id": 1, "username": "jure"}
