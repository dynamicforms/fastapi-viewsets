import asyncio
import fnmatch
import time

import pytest

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ..conf import settings
from ..context import Context
from ..decorators import route_viewset
from ..exceptions import (
    df_viewset_exception_handler,
    DfViewSetError,
    IdempotencyKeyInvalidError,
    IdempotencyKeyReusedError,
    NotFoundError,
)
from . import ViewSetResult
from .idempotency import Idempotency, parse_idempotency_key, request_fingerprint

KEY = '"8e03978e-40d5-43e8-bc93-6894a57f9324"'


@pytest.fixture(autouse=True)
def reset_settings():
    yield
    settings.viewsets_command_middleware = []
    settings.viewsets_idempotency_ttl = 86400
    settings.viewsets_idempotency_lock_ttl = 600


def make_request(method: str = "POST", path: str = "/items", body: bytes = b"", key: str | None = KEY, query=b""):
    headers = [(b"idempotency-key", key.encode())] if key is not None else []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": method, "path": path, "query_string": query, "headers": headers}
    return Request(scope, receive)


class Counter:
    """A chain end that counts its calls and answers with the count."""

    def __init__(self, delay: float = 0.0, result: ViewSetResult | None = None, error: Exception | None = None):
        self.calls = 0
        self.delay = delay
        self.result = result
        self.error = error

    async def __call__(self) -> ViewSetResult:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result or ViewSetResult(body={"call": self.calls})


async def call(middleware: Idempotency, chain: Counter, **request) -> ViewSetResult:
    return await middleware(make_request(**request), object(), Context({}), chain)


# the header


@pytest.mark.parametrize(
    ("value", "key"),
    [
        ('"abc"', "abc"),
        ('  "abc"  ', "abc"),
        ('"a\\"b\\\\c"', 'a"b\\c'),
        ('"8e03978e-40d5-43e8-bc93-6894a57f9324"', "8e03978e-40d5-43e8-bc93-6894a57f9324"),
    ],
)
def test_a_structured_field_string_is_parsed(value, key):
    assert parse_idempotency_key(value) == key


@pytest.mark.parametrize("value", ["abc", '""', '"abc', '"abc";a=1', '"a\\nb"', '"é"', '"a"b"', ""])
def test_anything_else_is_invalid(value):
    with pytest.raises(IdempotencyKeyInvalidError) as exc_info:
        parse_idempotency_key(value)
    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "idempotency_key_invalid"


@pytest.mark.asyncio
async def test_depends_rejects_an_invalid_key_and_passes_a_valid_one():
    middleware = Idempotency()
    assert await middleware.depends(make_request(), object, Context({})) is None
    with pytest.raises(IdempotencyKeyInvalidError):
        await middleware.depends(make_request(key="abc"), object, Context({}))


@pytest.mark.asyncio
async def test_the_fingerprint_covers_method_path_query_and_body():
    base = await request_fingerprint(make_request(body=b"{}"))
    assert base == await request_fingerprint(make_request(body=b"{}"))
    assert base != await request_fingerprint(make_request(method="PUT", body=b"{}"))
    assert base != await request_fingerprint(make_request(path="/other", body=b"{}"))
    assert base != await request_fingerprint(make_request(query=b"a=1", body=b"{}"))
    assert base != await request_fingerprint(make_request(body=b'{"a": 1}'))


# in-process records


@pytest.mark.asyncio
async def test_a_call_without_a_key_runs_every_time():
    middleware, chain = Idempotency(), Counter()
    await call(middleware, chain, key=None)
    await call(middleware, chain, key=None)
    assert chain.calls == 2


@pytest.mark.asyncio
async def test_a_repeat_answers_with_the_stored_outcome():
    result = ViewSetResult(body={"id": 1}, headers={"x-a": "1"}, status_code=201)
    middleware, chain = Idempotency(), Counter(result=result)
    first = await call(middleware, chain)
    repeat = await call(middleware, chain)
    assert chain.calls == 1
    assert (repeat.body, repeat.headers, repeat.status_code) == (first.body, first.headers, first.status_code)
    assert repeat is not first


@pytest.mark.asyncio
async def test_a_key_reused_with_a_different_fingerprint_answers_422():
    middleware, chain = Idempotency(), Counter()
    await call(middleware, chain, body=b"{}")
    with pytest.raises(IdempotencyKeyReusedError) as exc_info:
        await call(middleware, chain, body=b'{"a": 1}')
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "idempotency_key_reused"
    assert exc_info.value.params == {"key": KEY.strip('"')}


@pytest.mark.asyncio
async def test_repeats_of_a_running_call_attach_to_it():
    middleware, chain = Idempotency(), Counter(delay=0.05)
    results = await asyncio.gather(*(call(middleware, chain) for _ in range(3)))
    assert chain.calls == 1
    assert [result.body for result in results] == [{"call": 1}] * 3


@pytest.mark.asyncio
async def test_an_error_outcome_is_stored_and_raised_again():
    middleware, chain = Idempotency(), Counter(error=NotFoundError(7))
    with pytest.raises(NotFoundError):
        await call(middleware, chain)
    with pytest.raises(DfViewSetError) as exc_info:
        await call(middleware, chain)
    assert chain.calls == 1
    assert (exc_info.value.status_code, exc_info.value.code, exc_info.value.params) == (404, "not_found", {"pk": 7})


@pytest.mark.asyncio
async def test_an_unhandled_exception_is_replayed_as_a_500():
    middleware, chain = Idempotency(), Counter(error=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await call(middleware, chain)
    with pytest.raises(HTTPException) as exc_info:
        await call(middleware, chain)
    assert chain.calls == 1
    assert (exc_info.value.status_code, exc_info.value.detail) == (500, "Internal Server Error")


@pytest.mark.asyncio
async def test_a_finished_get_runs_again():
    middleware, chain = Idempotency(), Counter()
    await call(middleware, chain, method="GET")
    await call(middleware, chain, method="GET")
    assert chain.calls == 2


@pytest.mark.asyncio
async def test_a_get_a_repeat_attached_to_is_stored():
    middleware, chain = Idempotency(), Counter(delay=0.05)
    await asyncio.gather(call(middleware, chain, method="GET"), call(middleware, chain, method="GET"))
    await call(middleware, chain, method="GET")
    assert chain.calls == 1


@pytest.mark.parametrize(
    "result",
    [ViewSetResult(body=None, cookies={"sessionid": "x"}), ViewSetResult(body=None, headers={"Set-Cookie": "a=b"})],
)
@pytest.mark.asyncio
async def test_an_outcome_that_sets_a_cookie_is_not_stored(result):
    middleware, chain = Idempotency(), Counter(result=result)
    await call(middleware, chain)
    await call(middleware, chain)
    assert chain.calls == 2


@pytest.mark.asyncio
async def test_a_repeat_attached_to_a_call_that_sets_a_cookie_runs_again():
    middleware, chain = Idempotency(), Counter(delay=0.05, result=ViewSetResult(body=None, cookies={"s": "x"}))
    await asyncio.gather(call(middleware, chain), call(middleware, chain))
    assert chain.calls == 2


@pytest.mark.asyncio
async def test_a_stored_outcome_expires_after_the_ttl():
    settings.viewsets_idempotency_ttl = 0.05
    middleware, chain = Idempotency(), Counter()
    await call(middleware, chain)
    await asyncio.sleep(0.06)
    await call(middleware, chain)
    assert chain.calls == 2


@pytest.mark.asyncio
async def test_a_repeat_runs_again_once_the_running_record_expires():
    settings.viewsets_idempotency_lock_ttl = 0.05
    middleware, chain = Idempotency(), Counter(delay=0.2)
    first = asyncio.ensure_future(call(middleware, chain))
    await asyncio.sleep(0.01)
    repeat = await call(middleware, chain)
    assert chain.calls == 2
    assert repeat.body == {"call": 2}
    await first


@pytest.mark.asyncio
async def test_a_cancelled_call_leaves_no_record():
    middleware, chain = Idempotency(), Counter(delay=1)
    first = asyncio.ensure_future(call(middleware, chain))
    await asyncio.sleep(0.01)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    chain.delay = 0
    assert (await call(middleware, chain)).body == {"call": 2}


# through the application


class Item(BaseModel):
    id: int
    name: str


def make_app() -> tuple[FastAPI, list]:
    created: list = []
    router = APIRouter()

    @route_viewset(router, "/items")
    class ItemViewSet:
        __router = APIRouter()

        @__router.post("", status_code=201)
        async def create(self, item: Item) -> Item:
            created.append(item)
            return item

    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(DfViewSetError, df_viewset_exception_handler)
    return app, created


def test_a_repeated_post_answers_like_the_first_and_runs_once():
    settings.viewsets_command_middleware = [Idempotency()]
    app, created = make_app()
    client = TestClient(app)
    first = client.post("/items", json={"id": 1, "name": "a"}, headers={"Idempotency-Key": KEY})
    repeat = client.post("/items", json={"id": 1, "name": "a"}, headers={"Idempotency-Key": KEY})
    assert (first.status_code, first.json()) == (201, {"id": 1, "name": "a"})
    assert (repeat.status_code, repeat.json()) == (201, {"id": 1, "name": "a"})
    assert len(created) == 1


def test_the_errors_carry_their_codes():
    settings.viewsets_command_middleware = [Idempotency()]
    app, created = make_app()
    client = TestClient(app)
    invalid = client.post("/items", json={"id": 1, "name": "a"}, headers={"Idempotency-Key": "plain"})
    client.post("/items", json={"id": 1, "name": "a"}, headers={"Idempotency-Key": KEY})
    reused = client.post("/items", json={"id": 2, "name": "b"}, headers={"Idempotency-Key": KEY})
    assert (invalid.status_code, invalid.json()["detail_code"]) == (400, "idempotency_key_invalid")
    assert (reused.status_code, reused.json()["detail_code"]) == (422, "idempotency_key_reused")
    assert len(created) == 1


# Redis records


class FakeRedis:
    """The subset of `redis.asyncio.Redis` Idempotency uses, with key expiry and pub/sub."""

    def __init__(self):
        self.values: dict[str, tuple[str, float]] = {}
        self.channels: dict[str, list[asyncio.Queue]] = {}

    def _live(self, key: str) -> str | None:
        entry = self.values.get(key)
        if entry is None or entry[1] <= time.monotonic():
            self.values.pop(key, None)
            return None
        return entry[0]

    async def set(self, key: str, value: str, nx: bool = False, px: int | None = None):
        if nx and self._live(key) is not None:
            return None
        self.values[key] = (value, time.monotonic() + px / 1000)
        return True

    async def get(self, key: str):
        value = self._live(key)
        return None if value is None else value.encode()

    async def pttl(self, key: str) -> int:
        if self._live(key) is None:
            return -2
        return int((self.values[key][1] - time.monotonic()) * 1000)

    async def eval(self, _script: str, _numkeys: int, key: str, attached: str, running, done, ttl_ms, mode, local):
        if self._live(key) != running:
            return 0
        store = mode == "always" or (mode == "attached" and (local == "1" or self._live(attached) is not None))
        self.values.pop(attached, None)
        if store:
            self.values[key] = (done, time.monotonic() + ttl_ms / 1000)
            return 2
        self.values.pop(key, None)
        return 1

    async def publish(self, channel: str, message: str) -> None:
        for pattern, queues in self.channels.items():
            if fnmatch.fnmatchcase(channel, pattern):
                for queue in queues:
                    queue.put_nowait(message)

    def pubsub(self):
        return FakePubSub(self)


class FakePubSub:
    def __init__(self, redis: FakeRedis):
        self.redis = redis
        self.queue: asyncio.Queue = asyncio.Queue()
        self.channel: str | None = None

    async def subscribe(self, channel: str) -> None:
        self.channel = channel
        self.redis.channels.setdefault(channel, []).append(self.queue)

    async def get_message(self, ignore_subscribe_messages: bool, timeout: float):
        assert ignore_subscribe_messages
        try:
            return {"type": "message", "data": await asyncio.wait_for(self.queue.get(), timeout)}
        except asyncio.TimeoutError:
            return None

    async def unsubscribe(self, channel: str) -> None:
        self.redis.channels[channel].remove(self.queue)

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_redis_a_repeat_on_another_process_answers_with_the_stored_outcome():
    redis = FakeRedis()
    chain = Counter(result=ViewSetResult(body=Item(id=1, name="a"), status_code=201))
    await call(Idempotency(redis_client=redis), chain)
    repeat = await call(Idempotency(redis_client=redis), chain)
    assert chain.calls == 1
    assert (repeat.body, repeat.status_code) == ({"id": 1, "name": "a"}, 201)


@pytest.mark.asyncio
async def test_redis_the_running_record_has_the_lock_ttl_and_the_outcome_the_ttl():
    redis = FakeRedis()
    middleware = Idempotency(redis_client=redis, key_prefix="i:")
    seen = {}

    async def chain():
        seen["lock"] = await redis.pttl("i:" + KEY.strip('"'))
        return ViewSetResult(body=None)

    await middleware(make_request(), object(), Context({}), chain)
    assert 599_000 < seen["lock"] <= 600_000
    assert 86_399_000 < await redis.pttl("i:" + KEY.strip('"')) <= 86_400_000


@pytest.mark.asyncio
async def test_redis_a_repeat_on_another_process_attaches_to_the_running_call():
    redis = FakeRedis()
    chain = Counter(delay=0.05)
    results = await asyncio.gather(
        call(Idempotency(redis_client=redis), chain), call(Idempotency(redis_client=redis), chain)
    )
    assert chain.calls == 1
    assert [result.body for result in results] == [{"call": 1}] * 2


@pytest.mark.asyncio
async def test_redis_a_get_attached_to_from_another_process_is_stored():
    redis = FakeRedis()
    chain = Counter(delay=0.05)
    await asyncio.gather(
        call(Idempotency(redis_client=redis), chain, method="GET"),
        call(Idempotency(redis_client=redis), chain, method="GET"),
    )
    await call(Idempotency(redis_client=redis), chain, method="GET")
    assert chain.calls == 1


@pytest.mark.asyncio
async def test_redis_a_finished_get_runs_again():
    redis = FakeRedis()
    middleware, chain = Idempotency(redis_client=redis), Counter()
    await call(middleware, chain, method="GET")
    await call(middleware, chain, method="GET")
    assert chain.calls == 2


@pytest.mark.asyncio
async def test_redis_an_attached_repeat_takes_over_once_the_running_record_expires():
    settings.viewsets_idempotency_lock_ttl = 0.05
    redis = FakeRedis()
    stuck, chain = Counter(delay=0.3, result=ViewSetResult(body="stuck")), Counter()
    first = asyncio.ensure_future(call(Idempotency(redis_client=redis), stuck))
    await asyncio.sleep(0.01)
    repeat = await call(Idempotency(redis_client=redis), chain)
    assert (chain.calls, repeat.body) == (1, {"call": 1})
    await first
    # the first call's outcome does not replace the record of the call that took the key over
    assert (await call(Idempotency(redis_client=redis), Counter())).body == {"call": 1}


@pytest.mark.asyncio
async def test_redis_an_error_outcome_crosses_processes():
    redis = FakeRedis()
    chain = Counter(error=NotFoundError(3))
    with pytest.raises(NotFoundError):
        await call(Idempotency(redis_client=redis), chain)
    with pytest.raises(DfViewSetError) as exc_info:
        await call(Idempotency(redis_client=redis), chain)
    assert (exc_info.value.status_code, exc_info.value.code, exc_info.value.params) == (404, "not_found", {"pk": 3})
