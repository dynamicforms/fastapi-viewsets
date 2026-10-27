"""
Topic forms. Client topics: a viewset topic (routed base path, starts with `/`) or a custom topic
(source name `[a-z0-9_.-]+`, optionally `:key`), neither with whitespace or control characters.
A scoped topic `@{name}:{key}` is a store topic only, which `is_valid_topic` and `validate_topic` refuse.
"""

import re

from typing import Any

SOURCE_NAME_PATTERN = re.compile(r"[a-z0-9_.-]+")
"""The form of a source name, matched in full."""

_VIEWSET_TOPIC = re.compile(r"/[^\s\x00-\x1f\x7f]*")
_CUSTOM_TOPIC = re.compile(r"([a-z0-9_.-]+)(?::([^\s\x00-\x1f\x7f]+))?")
_SCOPED_TOPIC = re.compile(r"@([^\s\x00-\x1f\x7f:]+):([^\s\x00-\x1f\x7f]+)")
_SCOPE_NAME = re.compile(r"[^\s\x00-\x1f\x7f:]+")
_SCOPE_KEY = re.compile(r"[^\s\x00-\x1f\x7f]+")


def is_viewset_topic(topic: Any) -> bool:
    """Whether `topic` has the viewset form: a string starting with `/`."""
    return isinstance(topic, str) and _VIEWSET_TOPIC.fullmatch(topic) is not None


def is_valid_topic(topic: Any) -> bool:
    """Whether `topic` has either form."""
    return isinstance(topic, str) and (
        _VIEWSET_TOPIC.fullmatch(topic) is not None or _CUSTOM_TOPIC.fullmatch(topic) is not None
    )


def validate_topic(topic: Any) -> str:
    """Returns `topic`; raises `ValueError` when it has neither form."""
    if not is_valid_topic(topic):
        raise ValueError(
            f"{topic!r} is not a topic: neither a path starting with / nor a source name with an optional key"
        )
    return topic


def split_custom_topic(topic: str) -> tuple[str, str | None] | None:
    """`(name, key)` of a custom topic, `key` None without one; None for a topic of any other form."""
    match = _CUSTOM_TOPIC.fullmatch(topic) if isinstance(topic, str) else None
    if match is None:
        return None
    return match.group(1), match.group(2)


def _scope_key_text(key: Any) -> str:
    """`key` as text. Raises `ValueError` for an invalid `str`, `TypeError` for a type other than `str`/`int`."""
    if isinstance(key, bool) or not isinstance(key, (int, str)):
        raise TypeError(f"a scope key is a str or an int, not {type(key).__name__}")
    text = str(key)
    if _SCOPE_KEY.fullmatch(text) is None:
        raise ValueError(f"{key!r} is not a scope key: a non-empty string without whitespace or control characters")
    return text


def scoped_topic(name: str, key: "str | int") -> str:
    """The store topic `@{name}:{key}`; raises as `_scope_name` and `_scope_key_text`."""
    return f"@{_scope_name(name)}:{_scope_key_text(key)}"


def _scope_name(name: Any) -> str:
    """Returns `name`; raises `ValueError` unless it is non-empty without whitespace, control chars or `:`."""
    if not isinstance(name, str) or _SCOPE_NAME.fullmatch(name) is None:
        raise ValueError(
            f"{name!r} is not a scope name: a non-empty string without whitespace, control characters or ':'"
        )
    return name


def is_scoped_topic(topic: Any) -> bool:
    """Whether `topic` has the scoped form `@{name}:{key}`."""
    return isinstance(topic, str) and _SCOPED_TOPIC.fullmatch(topic) is not None


def split_scoped_topic(topic: str) -> tuple[str, str] | None:
    """`(name, key)` of a scoped topic; None for a topic of any other form."""
    match = _SCOPED_TOPIC.fullmatch(topic) if isinstance(topic, str) else None
    if match is None:
        return None
    return match.group(1), match.group(2)


def _validate_published_topic(topic: Any) -> str:
    """Returns `topic`; raises `ValueError` when it has neither client form nor the scoped form."""
    if not (is_valid_topic(topic) or is_scoped_topic(topic)):
        raise ValueError(
            f"{topic!r} is not a topic: neither a path starting with /, a source name with an optional key, "
            f"nor a scoped topic @name:key"
        )
    return topic
