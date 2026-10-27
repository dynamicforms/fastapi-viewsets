"""
Records each viewset's muxws endpoint set, for diagnostics and `schema_for`; commands are served by
the application's own router (`fastapi_viewsets.routing`).
"""

import warnings

from typing import Any

from ..conf import settings
from ..routing import openapi_endpoint


class _Registration:
    __slots__ = ("cls", "base_path", "route_kwargs", "tags", "schema")

    def __init__(self, cls: type, base_path: str, route_kwargs: list[dict[str, Any]], tags: list[str] | None):
        self.cls = cls
        self.base_path = base_path
        self.route_kwargs = route_kwargs
        self.tags = tags
        self.schema = openapi_endpoint(route_kwargs)


_registrations: dict[str, _Registration] = {}


def _key(cls: type) -> str:
    """Identifies a viewset by where it is defined, so a reloaded module replaces its registration."""
    return f"{cls.__module__}.{cls.__qualname__}"


def resolve_register_muxws(endpoint: Any, viewset_level: bool | None) -> bool:
    """The endpoint's decision, else the viewset's, else the global default; None abstains."""
    for value in (
        getattr(endpoint, "__register_muxws__", None),
        viewset_level,
        settings.viewsets_register_muxws,
    ):
        if value is not None:
            return bool(value)
    return True


def resolve_register_rest(endpoint: Any, viewset_level: bool | None = None) -> bool:
    """The endpoint's decision, else the viewset's, else True; None abstains."""
    for value in (getattr(endpoint, "__register_rest__", None), viewset_level):
        if value is not None:
            return bool(value)
    return True


def register_viewset(
    cls: type,
    base_path: str,
    route_kwargs: list[dict[str, Any]],
    tags: list[str] | None = None,
) -> None:
    """
    Records a viewset's muxws-reachable endpoints; re-registering a class replaces its entry. Two
    viewsets sharing one base path emit a warning: routing picks the first, so the later one's
    endpoints are unreachable.
    """
    key = _key(cls)
    for other_key, registration in _registrations.items():
        if other_key != key and registration.base_path == base_path:
            warnings.warn(
                f"{cls.__name__} registers base_path {base_path!r}, which "
                f"{registration.cls.__name__} already uses - the later one's endpoints are "
                f"unreachable",
                stacklevel=2,
            )
    _registrations[key] = _Registration(cls, base_path, route_kwargs, tags)


def is_registered(cls: type) -> bool:
    return _key(cls) in _registrations


def registered_viewsets() -> dict[type, str]:
    """Registered viewset classes mapped to their base paths."""
    return {registration.cls: registration.base_path for registration in _registrations.values()}


def schema_for(base_path: str) -> dict[str, Any] | None:
    for registration in _registrations.values():
        if registration.base_path == base_path:
            return registration.schema()
    return None


def reset_registry() -> None:
    """Drops every registration; for tests."""
    _registrations.clear()
