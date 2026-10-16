"""
The two topic forms.

A viewset topic is the routed base path of a viewset, the `include_router` prefix followed by
`base_path`, and starts with `/`. A custom topic is a source name, `[a-z0-9_.-]+`, optionally
followed by `:` and a key. Neither form contains whitespace or a control character. The two forms
cannot name the same topic.
"""

import re

from typing import Any

SOURCE_NAME_PATTERN = re.compile(r"[a-z0-9_.-]+")
"""The form of a source name, matched in full."""

_VIEWSET_TOPIC = re.compile(r"/[^\s\x00-\x1f\x7f]*")
_CUSTOM_TOPIC = re.compile(r"([a-z0-9_.-]+)(?::([^\s\x00-\x1f\x7f]+))?")


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
