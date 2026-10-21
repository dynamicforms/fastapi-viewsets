import asyncio
import gc
import json
import logging
import subprocess
import sys
import threading

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
    assert store.channel == "ev:events"


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
async def test_a_subscription_made_before_start_is_pushed_to_once_the_reader_runs():
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
async def test_the_reader_subscribes_one_channel_whatever_the_subscriptions():
    redis = FakeRedis()
    store = RedisEventStore(redis)
    settings.viewsets_event_store = store
    await store.start()
    try:
        assert list(redis.channels) == ["events:events"]
        peer = FakePeer()
        await subscribe(peer, Context({}), "/a")
        await subscribe(peer, Context({}), "/b")
        await unsubscribe(peer, "/a")
        assert list(redis.channels) == ["events:events"]
        assert redis.channels["events:events"] == [store._pubsub]
        await publish("/a", Ping(n=1))
        await publish("/b", Ping(n=2))
        await settle()
        assert [(payload, headers[":topic"]) for payload, headers in peer.pushes] == [({"n": 2}, "/b")]
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
            await redis.publish("events:events", "not json")
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
    assert store.reader_running
    await store.stop()
    assert not store.reader_running
    assert pubsub.closed
    assert redis.channels["events:events"] == []
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
async def test_start_returns_once_redis_has_confirmed_the_subscribe():
    redis = FakeRedis()
    store = RedisEventStore(redis)
    redis.pubsub_held = True
    starting = asyncio.ensure_future(store.start())
    await asyncio.sleep(0.02)
    assert not starting.done()
    assert not store.reader_running
    redis.opened[0].release()
    await asyncio.wait_for(starting, 1.0)
    try:
        assert store.reader_running
        assert redis.channels["events:events"] == [store._pubsub]
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_start_raises_and_closes_the_connection_when_the_confirmation_does_not_arrive():
    redis = FakeRedis()
    store = RedisEventStore(redis, subscribe_timeout=0.05)
    redis.pubsub_held = True
    with pytest.raises(asyncio.TimeoutError):
        await store.start()
    assert not store.reader_running
    assert redis.opened[0].closed
    redis.pubsub_held = False
    await store.start()
    try:
        assert store.reader_running
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_a_reconnect_subscribes_the_events_channel_again():
    redis = FakeRedis()
    store = RedisEventStore(redis)
    settings.viewsets_event_store = store
    await store.start()
    try:
        peer = FakePeer()
        await subscribe(peer, Context({}), "/a")
        store._pubsub.lose_connection()
        await publish("/a", Ping(n=1))
        await settle()
        assert [payload for payload, _headers in peer.pushes] == [{"n": 1}]
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_a_scoped_event_published_in_another_process_reaches_every_client_form_of_its_scope():
    redis = FakeRedis()
    web, worker = RedisEventStore(redis), RedisEventStore(redis)
    settings.viewsets_event_store = web
    await web.start()
    try:
        by_header, by_path, other = FakePeer(), FakePeer(), FakePeer()
        await subscribe(by_header, Context({}), "/api/v1/tags", store_topic="@tags:7", scope="7")
        await subscribe(by_path, Context({}), "/orgs/7/tags", store_topic="@tags:7", scope="7")
        await subscribe(other, Context({}), "/orgs/8/tags", store_topic="@tags:8", scope="8")
        settings.viewsets_event_store = worker
        rev, prev = await publish("@tags:7", Ping(n=1))
        await settle()
        assert [headers for _payload, headers in by_header.pushes] == [
            {":event": "ping", ":version": 1, ":topic": "/api/v1/tags", ":rev": rev, ":prev": prev, ":scope": "7"}
        ]
        assert [headers[":topic"] for _payload, headers in by_path.pushes] == ["/orgs/7/tags"]
        assert other.pushes == []
        assert await web.revs(["@tags:7", "/api/v1/tags"]) == {"@tags:7": rev, "/api/v1/tags": 0}
    finally:
        await web.stop()


class FakeRedisClass:
    """Stands in for `redis.asyncio.Redis` in `PerLoopRedis`: `from_url` returns a `FakeRedis` that
    records its url, options and whether it was closed."""

    def __init__(self):
        self.made: list[FakeRedis] = []

    def from_url(self, url: str, **options):
        client = FakeRedis()
        client.url, client.options, client.closed = url, options, False

        async def aclose():
            client.closed = True

        client.aclose = aclose
        self.made.append(client)
        return client


def per_loop(**options) -> tuple[redis_module.PerLoopRedis, FakeRedisClass]:
    clients = FakeRedisClass()
    redis = redis_module.PerLoopRedis("redis://example.test/3", **options)
    redis._redis_class = clients
    return redis, clients


def test_per_loop_redis_makes_one_client_per_loop_and_closes_it_with_the_loop():
    redis, clients = per_loop()

    async def use() -> FakeRedis:
        first = redis.client()
        await redis.get("k")
        assert redis.client() is first
        return first

    one = asyncio.run(use())
    two = asyncio.run(use())
    assert one is not two
    assert clients.made == [one, two]
    assert one.closed
    assert two.closed
    assert redis._clients == {}


def test_per_loop_redis_applies_the_default_options_under_the_given_ones():
    redis, clients = per_loop(socket_timeout=1.0, decode_responses=True)

    async def use() -> None:
        redis.client()

    asyncio.run(use())
    assert clients.made[0].url == "redis://example.test/3"
    assert clients.made[0].options == {
        "socket_connect_timeout": 2.0,
        "socket_timeout": 1.0,
        "decode_responses": True,
    }


def test_per_loop_redis_forgets_the_client_of_a_loop_closed_without_cancelling_its_tasks():
    redis, clients = per_loop()
    loop = asyncio.new_event_loop()

    async def use() -> None:
        redis.client()

    loop.run_until_complete(use())
    loop.close()
    asyncio.run(use())
    assert list(redis._clients) == []
    assert len(clients.made) == 2


def test_per_loop_redis_creates_a_client_while_the_garbage_collector_destroys_a_closed_loops_task():
    redis, clients = per_loop()
    make = clients.from_url

    def from_url_collecting(url: str, **options):
        gc.collect()
        return make(url, **options)

    clients.from_url = from_url_collecting
    loop = asyncio.new_event_loop()

    async def use() -> None:
        redis.client()

    loop.run_until_complete(use())
    loop.close()
    thread = threading.Thread(target=asyncio.run, args=(use(),), daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert list(redis._clients) == []
    assert len(clients.made) == 2


def test_per_loop_redis_outside_a_running_loop_raises():
    redis, _ = per_loop()
    with pytest.raises(RuntimeError):
        redis.client()


@pytest.mark.asyncio
async def test_from_url_builds_a_store_on_a_per_loop_client():
    store = RedisEventStore.from_url(
        "redis://example.test/3", key_prefix="ev:", subscribe_timeout=0.5, read_timeout=0.2, socket_timeout=1.0
    )
    assert isinstance(store.redis_client, redis_module.PerLoopRedis)
    assert store.redis_client.url == "redis://example.test/3"
    assert store.redis_client.options["socket_timeout"] == 1.0
    assert (store.key_prefix, store.subscribe_timeout, store.read_timeout) == ("ev:", 0.5, 0.2)
    clients = FakeRedisClass()
    store.redis_client._redis_class = clients
    await store.start()
    try:
        rev, _ = await store.append("t", "ping", {}, 1)
        assert await store.revs(["t"]) == {"t": rev}
        assert len(clients.made) == 1
    finally:
        await store.stop()


def test_per_loop_redis_on_a_loop_alive_at_exit_reports_nothing():
    script = (
        "import asyncio\n"
        "from fastapi_viewsets.events.redis import PerLoopRedis\n"
        "redis = PerLoopRedis('redis://127.0.0.1:1/0')\n"
        "async def use():\n"
        "    redis.client()\n"
        "loop = asyncio.new_event_loop()\n"
        "loop.run_until_complete(use())\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False, timeout=30)  # noqa: S603
    assert (result.returncode, result.stderr) == (0, "")


def test_per_loop_redis_does_not_forward_private_names():
    redis, _ = per_loop()
    assert not hasattr(redis, "_missing")
    assert not hasattr(redis, "__deepcopy__")
