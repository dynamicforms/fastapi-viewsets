import logging

from typing import ClassVar

import pytest

from ..conf import settings
from ..context import Context
from .conftest import FakePeer
from .delivery import subscribe, wait_for_pushes
from .event import Event
from .sources import declared_source, publish, reset_sources, Source, source
from .store import get_store, LoggedEvent, MemoryEventStore


class ImportFinished(Event):
    event_name = "import_finished"

    file: str
    rows: int


class ImportFinishedV2(Event):
    event_name = "import_finished"
    event_version = 2

    file: str
    rows: int
    skipped: int


def test_an_event_without_event_name_raises_type_error():
    with pytest.raises(TypeError, match="Nameless sets no event_name"):

        class Nameless(Event):
            value: int


def test_an_event_with_an_empty_event_name_raises_type_error():
    with pytest.raises(TypeError):

        class Empty(Event):
            event_name = ""


def test_event_name_is_a_class_variable_and_not_a_field():
    assert ImportFinished.event_name == "import_finished"
    assert "event_name" not in ImportFinished.model_fields
    assert ImportFinished(file="a.csv", rows=1).model_dump(mode="json") == {"file": "a.csv", "rows": 1}


def test_a_subclass_of_an_event_inherits_its_event_name():
    class Partial(ImportFinished):
        partial: bool = True

    assert Partial.event_name == "import_finished"


def test_an_event_has_version_1_unless_it_sets_a_larger_one():
    assert ImportFinished.event_version == 1
    assert ImportFinishedV2.event_version == 2
    assert "event_version" not in ImportFinishedV2.model_fields
    assert ImportFinishedV2(file="a.csv", rows=1, skipped=0).model_dump(mode="json") == {
        "file": "a.csv",
        "rows": 1,
        "skipped": 0,
    }


def test_a_subclass_of_an_event_inherits_its_event_version():
    class Partial(ImportFinishedV2):
        partial: bool = True

    assert Partial.event_version == 2


@pytest.mark.parametrize("version", [0, -1, True, False, 1.0, "2", None])
def test_an_event_version_other_than_an_int_of_at_least_1_raises_type_error(version):
    with pytest.raises(TypeError, match="Misversioned sets event_version to"):

        class Misversioned(Event):
            event_name = "misversioned"
            event_version = version


def test_an_event_that_declares_event_name_as_a_field_raises_type_error():
    with pytest.raises(TypeError, match="AnnotatedName declares event_name as a field"):

        class AnnotatedName(Event):
            event_name: str = "annotated"


@pytest.mark.filterwarnings("ignore:Field name .* shadows an attribute in parent")
def test_an_event_that_declares_event_version_as_a_field_raises_type_error():
    with pytest.raises(TypeError, match="AnnotatedVersion declares event_version as a field"):

        class AnnotatedVersion(Event):
            event_name = "annotated"
            event_version: int = 2


def test_an_event_with_a_class_var_event_version_is_accepted():
    class Annotated(Event):
        event_name: ClassVar[str] = "annotated"
        event_version: ClassVar[int] = 2

    assert Annotated.event_version == 2
    assert Annotated.model_fields == {}


@pytest.mark.asyncio
async def test_publish_records_the_event_and_returns_rev_and_prev():
    store = MemoryEventStore()
    settings.viewsets_event_store = store
    first = await publish("imports:7", ImportFinished(file="a.csv", rows=1))
    second = await publish("imports:7", ImportFinished(file="b.csv", rows=2))
    assert first[1] == 0
    assert second[1] == first[0]
    assert await store.since("imports:7", 0) == [
        LoggedEvent(first[0], 0, "import_finished", {"file": "a.csv", "rows": 1}, 1),
        LoggedEvent(second[0], first[0], "import_finished", {"file": "b.csv", "rows": 2}, 1),
    ]


@pytest.mark.asyncio
async def test_publish_records_the_events_version():
    store = MemoryEventStore()
    settings.viewsets_event_store = store
    first, _ = await publish("imports", ImportFinished(file="a.csv", rows=1))
    second, _ = await publish("imports", ImportFinishedV2(file="b.csv", rows=2, skipped=1))
    assert await store.since("imports", 0) == [
        LoggedEvent(first, 0, "import_finished", {"file": "a.csv", "rows": 1}, 1),
        LoggedEvent(second, first, "import_finished", {"file": "b.csv", "rows": 2, "skipped": 1}, 2),
    ]


@pytest.mark.asyncio
async def test_publish_with_an_invalid_topic_raises_value_error_and_records_nothing():
    with pytest.raises(ValueError, match="is not a topic"):
        await publish("Not A Topic", ImportFinished(file="a.csv", rows=1))
    assert await get_store().revs(["Not A Topic"]) == {"Not A Topic": 0}


@pytest.mark.asyncio
async def test_publish_takes_an_event():
    with pytest.raises(TypeError):
        await publish("imports", {"file": "a.csv"})


@pytest.mark.asyncio
async def test_publish_pushes_to_the_subscribed_sockets_of_this_process():
    peer = FakePeer()
    await subscribe(peer, Context({}), "imports")
    rev, prev = await publish("imports", ImportFinished(file="a.csv", rows=3))
    await wait_for_pushes()
    assert peer.pushes == [
        (
            {"file": "a.csv", "rows": 3},
            {":event": "import_finished", ":version": 1, ":topic": "imports", ":rev": rev, ":prev": prev},
        )
    ]


class FailingPublishStore(MemoryEventStore):
    async def publish(self, *_args):
        raise ConnectionError("redis is gone")


@pytest.mark.asyncio
async def test_a_failing_store_publish_is_logged_and_the_event_stays_recorded(caplog):
    store = FailingPublishStore()
    settings.viewsets_event_store = store
    with caplog.at_level(logging.WARNING, logger="fastapi_viewsets.events.sources"):
        rev, _prev = await publish("imports", ImportFinished(file="a.csv", rows=1))
    assert "carrying import_finished" in caplog.text
    assert await store.revs(["imports"]) == {"imports": rev}


def test_a_source_is_declared_once_per_name():
    imports = source("imports")
    assert declared_source("imports") is imports
    with pytest.raises(ValueError, match="already declared"):
        source("imports")
    with pytest.raises(ValueError, match="already declared"):
        Source("imports")


@pytest.mark.parametrize("name", ["", "Imports", "imp orts", "imports:7", "/imports", None])
def test_a_source_name_of_another_form_raises_value_error(name):
    with pytest.raises(ValueError, match="is not a source name"):
        source(name)
    assert declared_source(name) is None


def test_a_source_keeps_its_authorizer():
    async def org_member(_request, _context, _topic):
        return True

    assert source("org", authorizer=org_member).authorizer is org_member
    assert source("other").authorizer is None


def test_reset_sources_forgets_every_source():
    source("imports")
    reset_sources()
    assert declared_source("imports") is None
    source("imports")


def test_a_source_names_its_topics():
    imports = source("imports")
    assert imports.topic() == "imports"
    assert imports.topic("7") == "imports:7"
    assert imports.topic(7) == "imports:7"
    with pytest.raises(ValueError, match="is not a topic"):
        imports.topic("a b")


@pytest.mark.asyncio
async def test_a_source_publishes_to_its_topic_with_and_without_a_key():
    store = MemoryEventStore()
    settings.viewsets_event_store = store
    imports = source("imports")
    keyed, _ = await imports.publish(ImportFinished(file="a.csv", rows=1), key="7")
    plain, _ = await imports.publish(ImportFinished(file="b.csv", rows=2))
    assert await store.revs(["imports:7", "imports"]) == {"imports:7": keyed, "imports": plain}


@pytest.mark.asyncio
async def test_drop_removes_a_keyed_topic_from_the_store():
    store = MemoryEventStore()
    settings.viewsets_event_store = store
    imports = source("imports")
    await imports.publish(ImportFinished(file="a.csv", rows=1), key="7")
    await imports.drop("7")
    assert await store.revs(["imports:7"]) == {"imports:7": 0}
    _rev, prev = await imports.publish(ImportFinished(file="a.csv", rows=1), key="7")
    assert prev == 0
