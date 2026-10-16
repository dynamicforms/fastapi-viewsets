import asyncio
import json
import logging

import pytest

from ..conf import settings
from ..context import Context
from . import redis as redis_module
from .conftest import FakePeer, FakeRedis
from .delivery import subscribe, unsubscribe, wait_for_pushes
from .event import Event
from .lifecycle import check_events, start_events, stop_events
from .redis import RedisEventStore
from .sources import publish
from .store import MemoryEventStore


class Ping(Event):
    event_name = "ping"

    n: int


class PingV2(Event):
    event_name = "ping"
    event_version = 2

    n: int
    unit: str


async def settle() -> None:
    """Lets the reader take the messages queued on the fake channels, then waits for the pushes."""
    await asyncio.sleep(0.02)
    await wait_for_pushes()


@pytest.mark.asyncio
async def test_the_keys_and_the_channel_are_under_the_prefix():
    redis = FakeRedis()
    store = RedisEventStore(redis, key_prefix="ev:")
    rev, prev = await store.append("imports:7", "ping", {"n": 1}, 2)
    assert set(redis.strings) == {"ev:rev:imports:7"}
    assert set(redis.lists) == {"ev:log:imports:7"}
    assert json.loads(redis.lists["ev:log:imports:7"][0]) == {
        "rev": rev,
        "prev": prev,
        "event": {"name": "ping", "payload": {"n": 1}, "version": 2},
    }
    assert store.channel("imports:7") == "ev:ch:imports:7"


@pytest.mark.asyncio
async def test_the_counter_has_no_expiry_and_the_log_has_the_log_ttl():
    settings.viewsets_event_log_ttl = 30
    redis = FakeRedis()
    store = RedisEventStore(redis)
    await store.append("t", "ping", {}, 1)
    assert await redis.pttl("events:rev:t") == -1
    assert 29_000 < await redis.pttl("events:log:t") <= 30_000


@pytest.mark.asyncio
async def test_append_is_one_script_and_revs_one_mget():
    redis = FakeRedis()
    store = RedisEventStore(redis)
    await store.append("t", "ping", {}, 1)
    await store.revs(["t", "u"])
    assert redis.commands == ["EVAL", "MGET"]


@pytest.mark.asyncio
async def test_an_event_published_in_another_process_reaches_this_processs_sockets():
    redis = FakeRedis()
    web, worker = RedisEventStore(redis), RedisEventStore(redis)
    settings.viewsets_event_store = web
    await web.start()
    try:
        peer = FakePeer()
        await subscribe(peer, Context({}), "imports:7")
        settings.viewsets_event_store = worker
        rev, prev = await publish("imports:7", Ping(n=1))
        await settle()
        assert peer.pushes == [
            ({"n": 1}, {":event": "ping", ":version": 1, ":topic": "imports:7", ":rev": rev, ":prev": prev})
        ]
    finally:
        await web.stop()


@pytest.mark.asyncio
async def test_the_channel_message_carries_the_version_to_the_other_process():
    redis = FakeRedis()
    web, worker = RedisEventStore(redis), RedisEventStore(redis)
    settings.viewsets_event_store = web
    await web.start()
    try:
        peer = FakePeer()
        await subscribe(peer, Context({}), "imports:7")
        sent = []
        publish_to_redis = redis.publish

        async def recording_publish(channel, message):
            sent.append(json.loads(message))
            return await publish_to_redis(channel, message)

        redis.publish = recording_publish
        settings.viewsets_event_store = worker
        rev, prev = await publish("imports:7", PingV2(n=1, unit="s"))
        await settle()
        payload = {"n": 1, "unit": "s"}
        assert sent == [
            {"topic": "imports:7", "rev": rev, "prev": prev, "name": "ping", "payload": payload, "version": 2}
        ]
        assert peer.pushes == [
            (payload, {":event": "ping", ":version": 2, ":topic": "imports:7", ":rev": rev, ":prev": prev})
        ]
    finally:
        await web.stop()


@pytest.mark.asyncio
async def test_a_topic_watched_before_start_is_subscribed_when_the_reader_opens():
    redis = FakeRedis()
    store = RedisEventStore(redis)
    settings.viewsets_event_store = store
    peer = FakePeer()
    await subscribe(peer, Context({}), "/api/music")
    assert redis.channels == {}
    await start_events()
    try:
        await publish("/api/music", Ping(n=2))
        await settle()
        assert [payload for payload, _headers in peer.pushes] == [{"n": 2}]
    finally:
        await stop_events()


@pytest.mark.asyncio
async def test_an_unwatched_topic_is_no_longer_read():
    redis = FakeRedis()
    store = RedisEventStore(redis)
    settings.viewsets_event_store = store
    await store.start()
    try:
        peer = FakePeer()
        await subscribe(peer, Context({}), "/a")
        assert redis.channels["events:ch:/a"]
        await unsubscribe(peer, "/a")
        assert redis.channels["events:ch:/a"] == []
        await publish("/a", Ping(n=1))
        await settle()
        assert peer.pushes == []
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_a_message_that_cannot_be_read_is_logged_and_the_reader_carries_on(caplog):
    redis = FakeRedis()
    store = RedisEventStore(redis)
    settings.viewsets_event_store = store
    await store.start()
    try:
        peer = FakePeer()
        await subscribe(peer, Context({}), "/a")
        with caplog.at_level(logging.ERROR, logger="fastapi_viewsets.events.redis"):
            await redis.publish("events:ch:/a", "not json")
            await settle()
        assert "could not be delivered" in caplog.text
        await publish("/a", Ping(n=3))
        await settle()
        assert [payload for payload, _headers in peer.pushes] == [{"n": 3}]
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_stop_ends_the_reader_and_closes_its_connection():
    redis = FakeRedis()
    store = RedisEventStore(redis)
    await store.start()
    pubsub = store._pubsub
    await store.watch("/a")
    assert store.reader_running
    await store.stop()
    assert not store.reader_running
    assert pubsub.closed
    assert redis.channels["events:ch:/a"] == []
    await store.stop()


@pytest.mark.asyncio
async def test_start_twice_keeps_one_reader():
    store = RedisEventStore(FakeRedis())
    await store.start()
    reader = store._reader
    await store.start()
    assert store._reader is reader
    await store.stop()


@pytest.mark.asyncio
async def test_check_events_warns_while_a_redis_store_has_no_reader(caplog):
    store = RedisEventStore(FakeRedis())
    settings.viewsets_event_store = store
    with caplog.at_level(logging.WARNING, logger="fastapi_viewsets.events.lifecycle"):
        assert check_events() is False
    assert "start_events()" in caplog.text
    caplog.clear()
    await start_events()
    try:
        with caplog.at_level(logging.WARNING, logger="fastapi_viewsets.events.lifecycle"):
            assert check_events() is True
        assert caplog.text == ""
    finally:
        await stop_events()


def test_check_events_accepts_a_memory_store(caplog):
    settings.viewsets_event_store = MemoryEventStore()
    with caplog.at_level(logging.WARNING):
        assert check_events() is True
    settings.viewsets_event_store = None
    assert check_events() is True
    assert caplog.text == ""


@pytest.mark.asyncio
async def test_watch_returns_once_redis_has_confirmed_the_subscribe():
    redis = FakeRedis()
    store = RedisEventStore(redis)
    settings.viewsets_event_store = store
    await store.start()
    try:
        store._pubsub.held = True
        peer = FakePeer()
        subscribing = asyncio.ensure_future(subscribe(peer, Context({}), "/a"))
        await asyncio.sleep(0.02)
        assert not subscribing.done()
        assert redis.channels == {}

        store._pubsub.release()
        await asyncio.wait_for(subscribing, 1.0)
        await publish("/a", Ping(n=1))
        await settle()
        assert [payload for payload, _headers in peer.pushes] == [{"n": 1}]
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_watch_of_a_topic_with_an_unconfirmed_subscribe_waits_for_it():
    store = RedisEventStore(FakeRedis())
    await store.start()
    try:
        store._pubsub.held = True
        first = asyncio.ensure_future(store.watch("/a"))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(store.watch("/a"))
        await asyncio.sleep(0.02)
        assert not first.done()
        assert not second.done()
        store._pubsub.release()
        await asyncio.wait_for(asyncio.gather(first, second), 1.0)
        await asyncio.wait_for(store.watch("/a"), 0.1)
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_watch_raises_when_the_confirmation_does_not_arrive_in_time(monkeypatch):
    monkeypatch.setattr(redis_module, "_SUBSCRIBE_TIMEOUT", 0.05)
    store = RedisEventStore(FakeRedis())
    await store.start()
    try:
        store._pubsub.held = True
        with pytest.raises(asyncio.TimeoutError):
            await store.watch("/a")
        store._pubsub.release()
        await asyncio.sleep(0.02)
        await asyncio.wait_for(store.watch("/a"), 0.1)
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_a_subscribe_whose_confirmation_does_not_arrive_in_time_leaves_no_channel(monkeypatch):
    monkeypatch.setattr(redis_module, "_SUBSCRIBE_TIMEOUT", 0.05)
    redis = FakeRedis()
    store = RedisEventStore(redis)
    settings.viewsets_event_store = store
    await store.start()
    try:
        store._pubsub.held = True
        peer = FakePeer()
        with pytest.raises(asyncio.TimeoutError):
            await subscribe(peer, Context({}), "/a")
        store._pubsub.release()
        await asyncio.sleep(0.02)
        assert redis.channels.get(store.channel("/a"), []) == []
        assert store._listening == set()
        assert store._watched == set()

        await asyncio.wait_for(subscribe(peer, Context({}), "/a"), 1.0)
        await publish("/a", Ping(n=1))
        await settle()
        assert [payload for payload, _headers in peer.pushes] == [{"n": 1}]
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_watch_raises_when_the_reader_stops_before_the_confirmation():
    store = RedisEventStore(FakeRedis())
    await store.start()
    store._pubsub.held = True
    watching = asyncio.ensure_future(store.watch("/a"))
    await asyncio.sleep(0.01)
    await store.stop()
    with pytest.raises(ConnectionError):
        await watching
