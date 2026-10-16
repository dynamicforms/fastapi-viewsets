"""The store contract, run against `MemoryEventStore`, `RedisEventStore` on a fake Redis, and
`RedisEventStore` on the Redis named by FASTAPI_VIEWSETS_TEST_REDIS_URL (skipped without it)."""

import asyncio
import os
import uuid

import pytest
import pytest_asyncio

from ..conf import settings
from . import store as store_module
from .conftest import FakeRedis
from .redis import RedisEventStore
from .store import get_store, LoggedEvent, MAX_REV, MemoryEventStore

NOW = 1_800_000_000_000


@pytest_asyncio.fixture(params=["memory", "fake-redis", "redis"])
async def store(request):
    if request.param == "memory":
        yield MemoryEventStore()
        return
    if request.param == "fake-redis":
        yield RedisEventStore(FakeRedis())
        return
    url = os.environ.get("FASTAPI_VIEWSETS_TEST_REDIS_URL")
    if not url:
        pytest.skip("FASTAPI_VIEWSETS_TEST_REDIS_URL is not set")
    from redis.asyncio import Redis

    client = Redis.from_url(url)
    prefix = f"events-test-{uuid.uuid4().hex}:"
    yield RedisEventStore(client, key_prefix=prefix)
    keys = [key async for key in client.scan_iter(match=prefix + "*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """Sets the store's clock, in milliseconds; `clock.now` is the time an append sees."""

    class Clock:
        now = NOW

    monkeypatch.setattr(store_module, "_now_ms", lambda: Clock.now)
    return Clock


@pytest.mark.asyncio
async def test_the_first_event_of_a_topic_has_rev_now_and_prev_0(store):
    assert await store.append("/api/music", "records_changed", {"a": 1}) == (NOW, 0)


@pytest.mark.asyncio
async def test_two_events_in_one_millisecond_receive_consecutive_revs(store):
    first = await store.append("t", "e", 1)
    second = await store.append("t", "e", 2)
    third = await store.append("t", "e", 3)
    assert (first, second, third) == ((NOW, 0), (NOW + 1, NOW), (NOW + 2, NOW + 1))


@pytest.mark.asyncio
async def test_the_rev_follows_the_clock_once_the_clock_is_ahead(store, clock):
    await store.append("t", "e", 1)
    clock.now = NOW + 500
    assert await store.append("t", "e", 2) == (NOW + 500, NOW)


@pytest.mark.asyncio
async def test_a_clock_behind_the_counter_does_not_lower_the_rev(store, clock):
    await store.append("t", "e", 1)
    clock.now = NOW - 10_000
    assert await store.append("t", "e", 2) == (NOW + 1, NOW)


@pytest.mark.asyncio
async def test_topics_have_independent_revs(store):
    await store.append("a", "e", 1)
    await store.append("a", "e", 2)
    assert await store.append("b", "e", 1) == (NOW, 0)


@pytest.mark.asyncio
async def test_concurrent_appends_receive_distinct_ordered_revs(store):
    results = await asyncio.gather(*(store.append("t", "e", index) for index in range(20)))
    revs = sorted(rev for rev, _prev in results)
    assert revs == list(range(NOW, NOW + 20))
    chain = {prev: rev for rev, prev in results}
    rev, seen = chain[0], 1
    while rev in chain:
        rev, seen = chain[rev], seen + 1
    assert seen == 20


@pytest.mark.asyncio
async def test_append_raises_overflow_error_at_2_53_and_changes_nothing(store, clock):
    clock.now = MAX_REV - 1
    assert await store.append("t", "e", 1) == (MAX_REV - 1, 0)
    with pytest.raises(OverflowError):
        await store.append("t", "e", 2)
    assert await store.revs(["t"]) == {"t": MAX_REV - 1}
    assert await store.since("t", 0) == [LoggedEvent(MAX_REV - 1, 0, "e", 1)]


@pytest.mark.asyncio
async def test_revs_reads_every_topic_and_an_unknown_one_is_0(store):
    await store.append("a", "e", 1)
    await store.append("a", "e", 2)
    await store.append("/b", "e", 1)
    assert await store.revs(["a", "/b", "c"]) == {"a": NOW + 1, "/b": NOW, "c": 0}
    assert await store.revs([]) == {}


@pytest.mark.asyncio
async def test_since_the_current_rev_is_empty(store):
    assert await store.since("t", 0) == []
    await store.append("t", "e", 1)
    assert await store.since("t", NOW) == []


@pytest.mark.asyncio
async def test_since_answers_the_chain_from_the_cursor(store):
    for index in range(4):
        await store.append("t", "records_changed", {"index": index})
    assert await store.since("t", NOW + 1) == [
        LoggedEvent(NOW + 2, NOW + 1, "records_changed", {"index": 2}),
        LoggedEvent(NOW + 3, NOW + 2, "records_changed", {"index": 3}),
    ]
    assert [event.rev for event in await store.since("t", 0)] == [NOW, NOW + 1, NOW + 2, NOW + 3]


@pytest.mark.asyncio
async def test_since_a_cursor_no_kept_event_follows_is_none(store):
    settings.viewsets_event_log_size = 2
    for index in range(4):
        await store.append("t", "e", index)
    assert await store.since("t", NOW + 1) == [
        LoggedEvent(NOW + 2, NOW + 1, "e", 2),
        LoggedEvent(NOW + 3, NOW + 2, "e", 3),
    ]
    assert await store.since("t", NOW) is None
    assert await store.since("t", 0) is None
    assert await store.since("t", 12345) is None


@pytest.mark.asyncio
async def test_since_a_cursor_ahead_of_the_rev_is_none(store):
    await store.append("t", "e", 1)
    assert await store.since("t", NOW + 100) is None
    assert await store.since("unknown", NOW) is None


@pytest.mark.asyncio
async def test_with_log_size_0_every_cursor_behind_is_none(store):
    settings.viewsets_event_log_size = 0
    await store.append("t", "e", 1)
    await store.append("t", "e", 2)
    assert await store.since("t", NOW) is None
    assert await store.since("t", NOW + 1) == []
    assert await store.revs(["t"]) == {"t": NOW + 1}


@pytest.mark.asyncio
async def test_the_log_expires_and_the_counter_does_not(store, clock):
    settings.viewsets_event_log_ttl = 0.05
    await store.append("t", "e", 1)
    await store.append("t", "e", 2)
    assert await store.since("t", NOW) == [LoggedEvent(NOW + 1, NOW, "e", 2)]
    await asyncio.sleep(0.12)
    clock.now = NOW + 120
    assert await store.since("t", NOW) is None
    assert await store.since("t", NOW + 1) == []
    assert await store.revs(["t"]) == {"t": NOW + 1}
    assert await store.append("t", "e", 3) == (NOW + 120, NOW + 1)
    assert await store.since("t", NOW + 1) == [LoggedEvent(NOW + 120, NOW + 1, "e", 3)]


@pytest.mark.asyncio
async def test_drop_removes_the_counter_and_the_log(store, clock):
    await store.append("t:1", "e", 1)
    await store.append("t:1", "e", 2)
    await store.drop("t:1")
    assert await store.revs(["t:1"]) == {"t:1": 0}
    assert await store.since("t:1", NOW) is None
    clock.now = NOW + 50
    assert await store.append("t:1", "e", 3) == (NOW + 50, 0)


@pytest.mark.asyncio
async def test_payloads_round_trip_as_json_values(store):
    payload = {"op": "created", "changes": [{"pk": 1, "record": None}], "text": 'čšž "quoted"'}
    await store.append("t", "records_changed", payload)
    assert await store.since("t", 0) == [LoggedEvent(NOW, 0, "records_changed", payload)]


def test_without_a_configured_store_every_call_gets_one_memory_store():
    settings.viewsets_event_store = None
    assert isinstance(get_store(), MemoryEventStore)
    assert get_store() is get_store()
    configured = MemoryEventStore()
    settings.viewsets_event_store = configured
    assert get_store() is configured
