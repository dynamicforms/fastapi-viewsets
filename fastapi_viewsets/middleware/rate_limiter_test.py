import pytest

from fastapi import APIRouter, FastAPI
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
async def test_allows_requests_within_the_limit():
    limiter = RateLimiter(default_limit=2)
    context = Context({})
    viewset = _FakeViewset()

    first = await limiter(None, viewset, context, _final_ok)
    second = await limiter(None, viewset, context, _final_ok)

    assert first.body == "ok"
    assert second.body == "ok"


@pytest.mark.asyncio
async def test_rejects_with_429_once_the_limit_is_exceeded():
    limiter = RateLimiter(default_limit=1)
    context = Context({})
    viewset = _FakeViewset()

    await limiter(None, viewset, context, _final_ok)
    result = await limiter(None, viewset, context, _final_ok)

    assert result.status_code == 429
    assert result.body == {"detail": "Rate limit exceeded"}


@pytest.mark.asyncio
async def test_action_configuration_overrides_default_limit():
    limiter = RateLimiter(default_limit=100)
    context = Context({}, action_configuration={RateLimiter: 1})
    viewset = _FakeViewset()

    await limiter(None, viewset, context, _final_ok)
    result = await limiter(None, viewset, context, _final_ok)

    assert result.status_code == 429


@pytest.mark.asyncio
async def test_different_keys_are_tracked_independently():
    limiter = RateLimiter(default_limit=1, key_func=lambda request, _viewset, _context: request)

    first = await limiter("key-a", _FakeViewset(), Context({}), _final_ok)
    second = await limiter("key-b", _FakeViewset(), Context({}), _final_ok)

    assert first.body == "ok"
    assert second.body == "ok"  # different key - independent count, not blocked by key-a


@pytest.mark.asyncio
async def test_window_resets_after_window_seconds(monkeypatch):
    limiter = RateLimiter(default_limit=1, window_seconds=10)
    context = Context({})
    viewset = _FakeViewset()

    fake_now = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: fake_now[0])

    await limiter(None, viewset, context, _final_ok)
    blocked = await limiter(None, viewset, context, _final_ok)
    assert blocked.status_code == 429

    fake_now[0] += 11  # past the window
    allowed_again = await limiter(None, viewset, context, _final_ok)
    assert allowed_again.body == "ok"


@pytest.mark.asyncio
async def test_default_key_uses_viewset_type_and_client_ip():
    limiter = RateLimiter(default_limit=1)

    class _Client:
        host = "1.2.3.4"

    class _RequestStub:
        client = _Client

    key = limiter.key_func(_RequestStub(), _FakeViewset(), Context({}))
    assert key == "_FakeViewset:1.2.3.4"


@pytest.mark.asyncio
async def test_default_key_falls_back_to_unknown_without_a_request():
    limiter = RateLimiter(default_limit=1)
    key = limiter.key_func(None, _FakeViewset(), Context({}))
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
    viewset = _FakeViewset()

    first = await limiter(None, viewset, context, _final_ok)
    second = await limiter(None, viewset, context, _final_ok)
    third = await limiter(None, viewset, context, _final_ok)

    assert first.body == "ok"
    assert second.body == "ok"
    assert third.status_code == 429
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
    assert third.json() == {"detail": "Rate limit exceeded"}


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
