import pytest

from .sources import publish, source
from .store import get_store
from .topics import (
    is_scoped_topic,
    is_valid_topic,
    is_viewset_topic,
    scoped_topic,
    split_custom_topic,
    split_scoped_topic,
    validate_topic,
)
from .viewset_test import TrackAdded


@pytest.mark.parametrize(
    "topic", ["/api/music", "/", "/api/music/", "imports", "imports:7", "org.members-2_x", "imports:a:b", "a:/x"]
)
def test_a_topic_of_either_form_is_valid(topic):
    assert is_valid_topic(topic)
    assert validate_topic(topic) == topic


@pytest.mark.parametrize(
    "topic", ["", "Imports", "imports:", ":7", "imp orts", "/api music", "/api\tmusic", "imports:7 8", "/\x00", 7, None]
)
def test_anything_else_raises_value_error(topic):
    assert not is_valid_topic(topic)
    with pytest.raises(ValueError, match="is not a topic"):
        validate_topic(topic)


def test_the_viewset_form_starts_with_a_slash():
    assert is_viewset_topic("/api/music")
    assert not is_viewset_topic("imports:7")


def test_a_custom_topic_splits_into_name_and_key():
    assert split_custom_topic("imports:7") == ("imports", "7")
    assert split_custom_topic("imports") == ("imports", None)
    assert split_custom_topic("imports:a:b") == ("imports", "a:b")
    assert split_custom_topic("/api/music") is None


def test_the_scoped_form_is_a_store_topic_only():
    assert scoped_topic("tags", 7) == "@tags:7"
    assert scoped_topic("app.Tags", "a-b") == "@app.Tags:a-b"
    assert is_scoped_topic("@tags:7")
    assert split_scoped_topic("@tags:7:8") == ("tags", "7:8")
    assert split_scoped_topic("/api/tags") is None
    assert not is_scoped_topic("@tags")
    assert not is_valid_topic("@tags:7")
    with pytest.raises(ValueError, match="is not a topic"):
        validate_topic("@tags:7")


@pytest.mark.parametrize(
    ("name", "key", "error"),
    [
        ("", 7, ValueError),
        ("a:b", 7, ValueError),
        ("tags", "", ValueError),
        ("tags", "a b", ValueError),
        ("tags", True, TypeError),
        ("tags", None, TypeError),
    ],
)
def test_scoped_topic_refuses_a_name_or_key_out_of_form(name, key, error):
    with pytest.raises(error):
        scoped_topic(name, key)


@pytest.mark.asyncio
async def test_publish_accepts_the_scoped_form_and_a_source_does_not():
    rev, prev = await publish("@tags:7", TrackAdded(title="x"))
    assert prev == 0
    assert (await get_store().revs(["@tags:7"])) == {"@tags:7": rev}
    with pytest.raises(ValueError, match="is not a topic"):
        await publish("@tags", TrackAdded(title="x"))
    with pytest.raises(ValueError, match="is not a topic"):
        source("tags").topic("@x y")
