import pytest

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastapi_viewsets.action_configuration import action_configuration
from fastapi_viewsets.conf import settings
from fastapi_viewsets.context import Context
from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.middleware import ViewSetResult
from fastapi_viewsets.middleware.rate_limiter import RateLimiter
from fastapi_viewsets.mixins import ListMixin


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    settings.viewsets_command_middleware = []


async def _final_ok():
    return ViewSetResult(body="ok")


class _FakeViewset:
    pass


@pytest.mark.asyncio
async def test_call_is_a_trivial_passthrough():
    """All of RateLimiter's real logic lives in depends() now - __call__ just calls call_next()."""
    result = await RateLimiter(default_limit=1)(None, object(), Context({}), _final_ok)
    assert result.body == "ok"


@pytest.mark.asyncio
async def test_depends_allows_requests_within_the_limit():
    limiter = RateLimiter(default_limit=2)
    context = Context({})

    assert await limiter.depends(None, _FakeViewset, context) is None
    assert await limiter.depends(None, _FakeViewset, context) is None


@pytest.mark.asyncio
async def test_depends_rejects_with_429_once_the_limit_is_exceeded():
    limiter = RateLimiter(default_limit=1)
    context = Context({})

    await limiter.depends(None, _FakeViewset, context)
    with pytest.raises(HTTPException) as exc_info:
        await limiter.depends(None, _FakeViewset, context)

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["code"] == "rate_limited"
    assert exc_info.value.detail["message"] == "Rate limit exceeded"


@pytest.mark.asyncio
async def test_action_configuration_overrides_default_limit():
    limiter = RateLimiter(default_limit=100)
    context = Context({}, action_configuration={RateLimiter: 1})

    await limiter.depends(None, _FakeViewset, context)
    with pytest.raises(HTTPException) as exc_info:
        await limiter.depends(None, _FakeViewset, context)

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_different_keys_are_tracked_independently():
    limiter = RateLimiter(default_limit=1, key_func=lambda request, _cls, _context: request)

    assert await limiter.depends("key-a", _FakeViewset, Context({})) is None
    assert await limiter.depends("key-b", _FakeViewset, Context({})) is None  # different key - independent count


@pytest.mark.asyncio
async def test_async_key_func_is_awaited():
    async def key_func(request, _cls, _context):
        return request

    limiter = RateLimiter(default_limit=1, key_func=key_func)
    assert await limiter.depends("key-a", _FakeViewset, Context({})) is None
    assert await limiter.depends("key-b", _FakeViewset, Context({})) is None


@pytest.mark.asyncio
async def test_window_resets_after_window_seconds(monkeypatch):
    limiter = RateLimiter(default_limit=1, window_seconds=10)
    context = Context({})

    fake_now = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: fake_now[0])

    await limiter.depends(None, _FakeViewset, context)
    with pytest.raises(HTTPException):
        await limiter.depends(None, _FakeViewset, context)

    fake_now[0] += 11  # past the window
    assert await limiter.depends(None, _FakeViewset, context) is None


def test_default_key_uses_viewset_type_and_client_ip():
    limiter = RateLimiter(default_limit=1)

    class _Client:
        host = "1.2.3.4"

    class _RequestStub:
        client = _Client

    key = limiter.key_func(_RequestStub(), _FakeViewset, Context({}))
    assert key == "_FakeViewset:1.2.3.4"


def test_default_key_falls_back_to_unknown_without_a_request():
    limiter = RateLimiter(default_limit=1)
    key = limiter.key_func(None, _FakeViewset, Context({}))
    assert key == "_FakeViewset:unknown"


# ---------------------------------------------------------------------------
# Redis-backed storage
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self):
        self.counts = {}
        self.expired = []

    async def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, seconds):
        self.expired.append((key, seconds))


@pytest.mark.asyncio
async def test_redis_backend_increments_and_sets_expiry_on_first_hit():
    redis = _FakeRedis()
    limiter = RateLimiter(default_limit=2, window_seconds=30, redis_client=redis)
    context = Context({})

    await limiter.depends(None, _FakeViewset, context)
    await limiter.depends(None, _FakeViewset, context)
    with pytest.raises(HTTPException) as exc_info:
        await limiter.depends(None, _FakeViewset, context)

    assert exc_info.value.status_code == 429
    assert redis.expired == [("_FakeViewset:unknown", 30)]  # only set once, on the first increment


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


class Item(BaseModel):
    id: int
    name: str


def test_end_to_end_rate_limiting_via_route_viewset():
    settings.viewsets_command_middleware = [RateLimiter(default_limit=2)]

    router = APIRouter()

    @route_viewset(router, base_path="/items")
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context: Context) -> list[Item]:
            return [Item(id=1, name="Widget")]

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    assert client.get("/items").status_code == 200
    assert client.get("/items").status_code == 200
    third = client.get("/items")
    assert third.status_code == 429
    assert third.json() == {
        "detail": {"message": "Rate limit exceeded", "code": "rate_limited", "params": {}}
    }


def test_end_to_end_action_configuration_overrides_limit_per_viewset():
    settings.viewsets_command_middleware = [RateLimiter(default_limit=100)]

    router = APIRouter()

    @route_viewset(router, base_path="/strict-items")
    @action_configuration({RateLimiter: 1})
    class StrictItemViewSet(ListMixin[Item]):
        async def perform_list(self, _context: Context) -> list[Item]:
            return [Item(id=1, name="Widget")]

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    assert client.get("/strict-items").status_code == 200
    second = client.get("/strict-items")
    assert second.status_code == 429
