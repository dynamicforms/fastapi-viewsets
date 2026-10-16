import asyncio
import logging
import subprocess
import sys

from pathlib import Path
from typing import Any

import pytest

from ..conf import settings
from ..context import Context
from ..exceptions import EventSubscriptionsExceededError
from .conftest import ConnectionLost, FakePeer
from .delivery import deliver, drop_peer, subscribe, subscriptions, topics_of, unsubscribe, wait_for_pushes
from .store import MemoryEventStore


class RecordingStore(MemoryEventStore):
    def __init__(self, watch_error: BaseException | None = None):
        super().__init__()
        self.calls: list[tuple[str, str]] = []
        self.watch_error = watch_error

    async def watch(self, topic):
        self.calls.append(("watch", topic))
        if self.watch_error is not None:
            raise self.watch_error

    async def unwatch(self, topic):
        self.calls.append(("unwatch", topic))


@pytest.fixture(autouse=True)
def store():
    store = RecordingStore()
    settings.viewsets_event_store = store
    return store


async def deliver_and_wait(topic="/api/music", rev=2, prev=1, name="records_changed", payload=None, version=1):
    await deliver(topic, rev, prev, name, {"op": "created"} if payload is None else payload, version)
    await wait_for_pushes()


# the subscription table


@pytest.mark.asyncio
async def test_the_first_subscription_of_a_topic_watches_it_and_the_last_unsubscribe_unwatches_it(store):
    first, second = FakePeer(), FakePeer()
    await subscribe(first, Context({}), "/api/music")
    await subscribe(second, Context({}), "/api/music")
    assert store.calls == [("watch", "/api/music")]
    await unsubscribe(first, "/api/music")
    assert store.calls == [("watch", "/api/music")]
    await unsubscribe(second, "/api/music")
    assert store.calls == [("watch", "/api/music"), ("unwatch", "/api/music")]
    assert subscriptions("/api/music") == []


@pytest.mark.asyncio
async def test_a_subscription_holds_the_peer_the_context_and_the_topic():
    peer, context = FakePeer(), Context({"user": "ana"})
    subscription = await subscribe(peer, context, "imports:7")
    assert (subscription.peer, subscription.context, subscription.topic) == (peer, context, "imports:7")
    assert subscriptions("imports:7") == [subscription]
    assert topics_of(peer) == ["imports:7"]


@pytest.mark.asyncio
async def test_subscribing_a_held_topic_again_replaces_the_context_and_counts_once(store):
    peer = FakePeer()
    await subscribe(peer, Context({"n": 1}), "/a")
    again = await subscribe(peer, Context({"n": 2}), "/a")
    assert subscriptions("/a") == [again]
    assert topics_of(peer) == ["/a"]
    assert store.calls == [("watch", "/a")]


@pytest.mark.asyncio
async def test_unsubscribing_a_topic_the_socket_does_not_hold_does_nothing(store):
    peer = FakePeer()
    await unsubscribe(peer, "/a")
    await subscribe(FakePeer(), Context({}), "/a")
    await unsubscribe(peer, "/a")
    assert len(subscriptions("/a")) == 1
    assert store.calls == [("watch", "/a")]


@pytest.mark.asyncio
async def test_one_close_handler_is_installed_per_socket():
    peer = FakePeer()
    await subscribe(peer, Context({}), "/a")
    await subscribe(peer, Context({}), "/b")
    await unsubscribe(peer, "/a")
    await unsubscribe(peer, "/b")
    await subscribe(peer, Context({}), "/c")
    assert len(peer.close_handlers) == 1


@pytest.mark.asyncio
async def test_a_lost_socket_drops_every_subscription_it_held(store):
    lost, kept = FakePeer(), FakePeer()
    await subscribe(lost, Context({}), "/a")
    await subscribe(lost, Context({}), "/b")
    await subscribe(kept, Context({}), "/b")
    lost.close()
    await wait_for_pushes()
    assert topics_of(lost) == []
    assert [subscription.peer for subscription in subscriptions("/b")] == [kept]
    assert sorted(store.calls) == [("unwatch", "/a"), ("watch", "/a"), ("watch", "/b")]


@pytest.mark.asyncio
async def test_a_socket_subscribing_after_its_drop_installs_a_new_close_handler():
    peer = FakePeer()
    await subscribe(peer, Context({}), "/a")
    drop_peer(peer)
    await subscribe(peer, Context({}), "/a")
    assert len(peer.close_handlers) == 2


@pytest.mark.asyncio
async def test_a_socket_holds_at_most_max_subscriptions_topics():
    settings.viewsets_event_max_subscriptions = 2
    peer = FakePeer()
    await subscribe(peer, Context({}), "/a")
    await subscribe(peer, Context({}), "/b")
    with pytest.raises(EventSubscriptionsExceededError) as exc_info:
        await subscribe(peer, Context({}), "/c")
    assert (exc_info.value.status_code, exc_info.value.code, exc_info.value.params) == (
        409,
        "event_subscriptions_exceeded",
        {"limit": 2},
    )
    await subscribe(peer, Context({}), "/b")
    await subscribe(FakePeer(), Context({}), "/c")
    assert topics_of(peer) == ["/a", "/b"]


@pytest.mark.asyncio
async def test_a_failing_watch_removes_the_subscription_unwatches_the_topic_and_raises():
    store = RecordingStore(watch_error=ConnectionError("redis is gone"))
    settings.viewsets_event_store = store
    peer = FakePeer()
    with pytest.raises(ConnectionError):
        await subscribe(peer, Context({}), "/a")
    assert subscriptions("/a") == []
    assert topics_of(peer) == []
    assert store.calls == [("watch", "/a"), ("unwatch", "/a")]


@pytest.mark.asyncio
async def test_an_unwatch_that_raises_after_a_failing_watch_is_logged(caplog):
    store = RecordingStore(watch_error=ConnectionError("redis is gone"))

    async def failing_unwatch(topic):
        store.calls.append(("unwatch", topic))
        raise ConnectionError("still gone")

    store.unwatch = failing_unwatch
    settings.viewsets_event_store = store
    with caplog.at_level(logging.WARNING), pytest.raises(ConnectionError, match="redis is gone"):
        await subscribe(FakePeer(), Context({}), "/a")
    assert store.calls == [("watch", "/a"), ("unwatch", "/a")]
    assert "after its watch failed" in caplog.text


@pytest.mark.asyncio
async def test_a_subscribe_during_an_unwatch_ends_watched(store):
    release = asyncio.Event()

    async def slow_unwatch(topic):
        store.calls.append(("unwatch", topic))
        await release.wait()

    store.unwatch = slow_unwatch
    first, second = FakePeer(), FakePeer()
    await subscribe(first, Context({}), "/a")
    leaving = asyncio.ensure_future(unsubscribe(first, "/a"))
    await asyncio.sleep(0)
    joining = asyncio.ensure_future(subscribe(second, Context({}), "/a"))
    await asyncio.sleep(0.01)
    assert not joining.done()
    release.set()
    await leaving
    await joining
    assert store.calls == [("watch", "/a"), ("unwatch", "/a"), ("watch", "/a")]


@pytest.mark.asyncio
async def test_a_subscribe_during_a_watch_returns_once_the_topic_is_watched(store):
    release = asyncio.Event()

    async def slow_watch(topic):
        store.calls.append(("watch", topic))
        await release.wait()

    store.watch = slow_watch
    first = asyncio.ensure_future(subscribe(FakePeer(), Context({}), "/a"))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(subscribe(FakePeer(), Context({}), "/a"))
    await asyncio.sleep(0.01)
    assert not first.done()
    assert not second.done()
    release.set()
    await asyncio.gather(first, second)
    assert store.calls == [("watch", "/a")]
    assert len(subscriptions("/a")) == 2


@pytest.mark.asyncio
async def test_a_subscribe_during_a_failing_watch_watches_the_topic_itself(store):
    attempts = []

    async def failing_once(topic):
        attempts.append(topic)
        await asyncio.sleep(0.01)
        if len(attempts) == 1:
            raise ConnectionError("redis hiccup")

    store.watch = failing_once
    first, second = FakePeer(), FakePeer()
    outcomes = await asyncio.gather(
        subscribe(first, Context({}), "/a"), subscribe(second, Context({}), "/a"), return_exceptions=True
    )
    assert isinstance(outcomes[0], ConnectionError)
    assert outcomes[1].peer is second
    assert attempts == ["/a", "/a"]
    assert [subscription.peer for subscription in subscriptions("/a")] == [second]
    assert topics_of(first) == []


@pytest.mark.asyncio
async def test_concurrent_subscribes_of_a_topic_whose_watch_fails_all_fail():
    settings.viewsets_event_store = RecordingStore(watch_error=ConnectionError("redis is gone"))
    peers = [FakePeer(), FakePeer()]
    outcomes = await asyncio.gather(*(subscribe(peer, Context({}), "/a") for peer in peers), return_exceptions=True)
    assert all(isinstance(outcome, ConnectionError) for outcome in outcomes)
    assert subscriptions("/a") == []


@pytest.mark.asyncio
async def test_a_closed_socket_is_not_subscribed(store):
    peer = FakePeer()
    await subscribe(peer, Context({}), "/a")
    peer.close()
    await wait_for_pushes()

    subscription = await subscribe(peer, Context({}), "imports:7")
    assert subscription.topic == "imports:7"
    assert subscriptions("imports:7") == []
    assert topics_of(peer) == []
    assert ("watch", "imports:7") not in store.calls
    assert len(peer.close_handlers) == 1


@pytest.mark.asyncio
async def test_the_first_subscribe_of_a_socket_closed_before_it_subscribes_nothing(store):
    peer = FakePeer()
    peer.close()
    await subscribe(peer, Context({}), "/a")
    assert subscriptions("/a") == []
    assert peer.close_handlers == []
    assert store.calls == []


# delivery


@pytest.mark.asyncio
async def test_deliver_pushes_one_notify_to_every_subscribed_socket():
    first, second, other = FakePeer(), FakePeer(), FakePeer()
    await subscribe(first, Context({}), "/api/music")
    await subscribe(second, Context({}), "/api/music")
    await subscribe(other, Context({}), "/api/other")
    await deliver_and_wait(rev=18, prev=17)
    expected = [
        (
            {"op": "created"},
            {":event": "records_changed", ":version": 1, ":topic": "/api/music", ":rev": 18, ":prev": 17},
        )
    ]
    assert first.pushes == expected
    assert second.pushes == expected
    assert other.pushes == []


@pytest.mark.asyncio
async def test_the_push_carries_the_events_version():
    peer = FakePeer()
    await subscribe(peer, Context({}), "imports")
    await deliver_and_wait(topic="imports", rev=5, prev=4, name="import_finished", payload={"rows": 2}, version=3)
    assert peer.pushes == [
        ({"rows": 2}, {":event": "import_finished", ":version": 3, ":topic": "imports", ":rev": 5, ":prev": 4})
    ]


@pytest.mark.asyncio
async def test_deliver_to_a_topic_without_subscriptions_does_nothing():
    await deliver_and_wait(topic="/nobody")


def slow_for(slow_peer: FakePeer, seconds: float):
    """A delivery filter that admits every push and takes `seconds` for the pushes to `slow_peer`."""

    async def slow(subscription, *_args):
        if subscription.peer is slow_peer:
            await asyncio.sleep(seconds)
        return True

    return slow


@pytest.mark.asyncio
async def test_deliver_returns_before_a_slow_push_completes():
    slow, fast = FakePeer(), FakePeer()
    await subscribe(slow, Context({}), "/a")
    await subscribe(fast, Context({}), "/a")
    settings.viewsets_event_delivery_filters = [slow_for(slow, 0.2)]
    await deliver("/a", 2, 1, "e", {}, 1)
    await asyncio.sleep(0.02)
    assert len(fast.pushes) == 1
    assert slow.pushes == []
    await wait_for_pushes()
    assert len(slow.pushes) == 1


@pytest.mark.asyncio
async def test_delivery_filters_count_toward_the_push_timeout(caplog):
    settings.viewsets_event_push_timeout = 0.05
    slow, fast = FakePeer(), FakePeer()
    await subscribe(slow, Context({}), "/a")
    await subscribe(fast, Context({}), "/a")
    settings.viewsets_event_delivery_filters = [slow_for(slow, 1.0)]
    with caplog.at_level(logging.DEBUG, logger="fastapi_viewsets.events.delivery"):
        await deliver_and_wait(topic="/a")
    assert slow.pushes == []
    assert len(fast.pushes) == 1
    assert [record.levelno for record in caplog.records] == [logging.DEBUG]
    assert topics_of(slow) == ["/a"]


@pytest.mark.asyncio
async def test_a_push_to_a_real_muxws_peer_reaches_the_other_end_and_a_lost_socket_drops_it(store):
    from muxws import accept, resolve_codec
    from muxws.peer import Peer
    from muxws.transports.memory import memory_pair

    server_socket, client_socket = memory_pair()
    server = await accept(server_socket)
    client = Peer(client_socket, codec=resolve_codec(None), is_dialer=True)
    received: list[tuple[Any, dict[str, Any]]] = []
    arrived = asyncio.Event()

    async def on_stream(payload, stream):
        received.append((payload, dict(stream.headers)))
        arrived.set()

    client.on_stream(on_stream)
    serving = [asyncio.ensure_future(server.serve()), asyncio.ensure_future(client.serve())]
    try:
        await subscribe(server, Context({}), "/api/music")
        await deliver_and_wait(topic="/api/music", rev=18, prev=17, payload={"op": "deleted"}, version=2)
        await asyncio.wait_for(arrived.wait(), 2.0)
        [(payload, headers)] = received
        assert payload == {"op": "deleted"}
        assert {key: headers[key] for key in (":event", ":version", ":topic", ":rev", ":prev")} == {
            ":event": "records_changed",
            ":version": 2,
            ":topic": "/api/music",
            ":rev": 18,
            ":prev": 17,
        }

        await client_socket.drop()
        for _ in range(100):
            if not server.is_open:
                break
            await asyncio.sleep(0.01)
        await wait_for_pushes()
        assert topics_of(server) == []
        assert ("unwatch", "/api/music") in store.calls

        await subscribe(server, Context({}), "/api/other")
        assert subscriptions("/api/other") == []
    finally:
        for task in serving:
            task.cancel()
        await asyncio.gather(*serving, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_failing_push_is_logged_at_debug_and_keeps_the_subscription(caplog):
    failing = FakePeer(error=RuntimeError("boom"))
    await subscribe(failing, Context({}), "/a")
    with caplog.at_level(logging.DEBUG, logger="fastapi_viewsets.events.delivery"):
        await deliver_and_wait(topic="/a")
    assert [record.levelno for record in caplog.records] == [logging.DEBUG]
    assert topics_of(failing) == ["/a"]


@pytest.mark.asyncio
async def test_connection_lost_drops_the_sockets_subscriptions(store):
    lost = FakePeer(error=ConnectionLost("socket died"))
    await subscribe(lost, Context({}), "/a")
    await subscribe(lost, Context({}), "/b")
    await deliver_and_wait(topic="/a")
    assert topics_of(lost) == []
    assert ("unwatch", "/b") in store.calls


@pytest.mark.asyncio
async def test_muxws_connection_lost_and_its_subclasses_are_recognised():
    from muxws.errors import ConnectionLost as MuxwsConnectionLost

    class Subclass(MuxwsConnectionLost):
        pass

    for error in (MuxwsConnectionLost("gone"), Subclass("gone")):
        peer = FakePeer(error=error)
        await subscribe(peer, Context({}), "/a")
        await deliver_and_wait(topic="/a")
        assert topics_of(peer) == []


@pytest.mark.asyncio
async def test_a_delivery_filter_that_answers_false_skips_the_socket():
    skipped, sent = FakePeer(), FakePeer()
    await subscribe(skipped, Context({"user": "skipped"}), "/a")
    await subscribe(sent, Context({"user": "sent"}), "/a")
    seen = []

    def by_user(subscription, topic, rev, name, payload, version):
        seen.append((subscription.topic, topic, rev, name, payload, version))
        return subscription.context.raw()["user"] != "skipped"

    settings.viewsets_event_delivery_filters = [by_user]
    await deliver_and_wait(topic="/a", rev=5, prev=4, name="e", payload={"x": 1}, version=2)
    assert skipped.pushes == []
    assert len(sent.pushes) == 1
    assert sorted(seen) == [("/a", "/a", 5, "e", {"x": 1}, 2)] * 2


@pytest.mark.asyncio
async def test_an_async_delivery_filter_is_awaited():
    peer = FakePeer()
    await subscribe(peer, Context({}), "/a")

    async def refuse(*_args):
        return False

    settings.viewsets_event_delivery_filters = [refuse]
    await deliver_and_wait(topic="/a")
    assert peer.pushes == []


@pytest.mark.asyncio
async def test_a_raising_delivery_filter_counts_as_true():
    peer = FakePeer()
    await subscribe(peer, Context({}), "/a")

    def broken(*_args):
        raise KeyError("user")

    def admit(*_args):
        return True

    settings.viewsets_event_delivery_filters = [broken, admit]
    await deliver_and_wait(topic="/a")
    assert len(peer.pushes) == 1


@pytest.mark.asyncio
async def test_every_filter_must_admit_the_push():
    peer = FakePeer()
    await subscribe(peer, Context({}), "/a")
    settings.viewsets_event_delivery_filters = [lambda *_: True, lambda *_: False]
    await deliver_and_wait(topic="/a")
    assert peer.pushes == []


def test_the_events_package_imports_neither_muxws_nor_redis():
    code = (
        "import sys\n"
        "import fastapi_viewsets.events\n"
        "assert 'muxws' not in sys.modules, 'muxws'\n"
        "assert 'redis' not in sys.modules, 'redis'\n"
    )
    root = Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable, "-c", code], cwd=root, check=True)  # noqa: S603
