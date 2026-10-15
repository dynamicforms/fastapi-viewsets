"""
Which viewset endpoints are published on muxws.

A command is served by the application's own router (see `fastapi_viewsets.routing`); this registry
records each viewset's muxws endpoint set, for diagnostics and `schema_for`.
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
    """
    Identifies a viewset by where it is defined rather than by object identity, so that reloading
    a module - which produces a brand new class object for the same viewset - replaces its
    registration instead of adding a shadowed duplicate.
    """
    return f"{cls.__module__}.{cls.__qualname__}"


def resolve_register_muxws(endpoint: Any, viewset_level: bool | None) -> bool:
    """
    Endpoint decision beats viewset decision beats the global default.

    Each level may abstain by leaving the answer None, in which case the question falls through to
    the next one out; only the global setting has to actually decide.
    """
    for value in (
        getattr(endpoint, "__register_muxws__", None),
        viewset_level,
        settings.viewsets_register_muxws,
    ):
        if value is not None:
            return bool(value)
    return True


def resolve_register_rest(endpoint: Any, viewset_level: bool | None = None) -> bool:
    """
    Endpoint decision beats viewset decision beats "yes" - the same ladder muxws uses.

    Symmetric with muxws: `route_viewset` registers a viewset on *both* transports, so a viewset that
    is muxws-only says so with `register_rest=False` rather than by annotating every endpoint.
    """
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
    Records a viewset's muxws-reachable endpoints. Re-registering the same class replaces its
    previous entry rather than adding a second copy, so re-importing a module (or a test that
    decorates the same viewset twice) cannot produce shadowed duplicate routes.

    Two different viewsets sharing one base path are warned about: routing picks the first match,
    so the later one's endpoints are simply unreachable, and neither FastAPI nor this library would
    otherwise say so. A warning rather than an error, because that is proportionate to how easily
    it is fixed and to how little else in this area is strict, and an application that registers
    such a pair still starts.

    It is noisy in a suite that builds a throwaway viewset per test; the suite filters it (see
    pyproject.toml) rather than the design absorbing the noise.
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
    """Registered viewset classes mapped to their base paths - handy for diagnostics."""
    return {registration.cls: registration.base_path for registration in _registrations.values()}


def schema_for(base_path: str) -> dict[str, Any] | None:
    for registration in _registrations.values():
        if registration.base_path == base_path:
            return registration.schema()
    return None


def reset_registry() -> None:
    """Drops every registration. Exists for tests; calling it in an application is a mistake."""
    _registrations.clear()
