import pytest

from .topics import is_valid_topic, is_viewset_topic, split_custom_topic, validate_topic


@pytest.mark.parametrize(
    "topic", ["/api/music", "/", "/api/music/", "imports", "imports:7", "club.members-2_x", "imports:a:b", "a:/x"]
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
