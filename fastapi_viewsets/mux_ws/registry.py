"""
Which viewset endpoints are reachable over muxws, and the FastAPI apps that serve them.

Two apps, for the same reason the REST side has two: one that everything is registered into and
that actually dispatches, and one per viewset that exists only so `/{base_path}/schema` has an
OpenAPI document to hand back. Keeping them separate is what lets the muxws schema legitimately
differ from the REST one - an endpoint can be registered for one transport and not the other, and
each `/schema` then describes exactly the transport it was asked over.
"""

import warnings

from typing import Any

from fastapi import FastAPI

from ..conf import settings


class _Registration:
    __slots__ = ("cls", "base_path", "route_kwargs", "tags", "_schema_app")

    def __init__(self, cls: type, base_path: str, route_kwargs: list[dict[str, Any]], tags: list[str] | None):
        self.cls = cls
        self.base_path = base_path
        self.route_kwargs = route_kwargs
        self.tags = tags
        self._schema_app: FastAPI | None = None

    def schema(self) -> dict[str, Any]:
        if self._schema_app is None:
            app = FastAPI()
            for kwargs in self.route_kwargs:
                app.router.add_api_route(**kwargs)
            self._schema_app = app
        return self._schema_app.openapi()


_registrations: dict[str, _Registration] = {}
_dispatch_app: FastAPI | None = None


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

    Symmetric on purpose. `route_viewset` is what registers a viewset on *both* transports, so not
    calling it turns off muxws as well; there was no way to say "this viewset is muxws-only" short
    of annotating every endpoint on it.
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
    it is fixed and to how little else in this area is strict - and because refusing would be a new
    way for an application that starts today to stop starting.

    It is noisy in a suite that builds a throwaway viewset per test; the suite filters it (see
    pyproject.toml) rather than the design absorbing the noise.
    """
    global _dispatch_app
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
    _dispatch_app = None


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


def get_dispatch_app() -> FastAPI:
    """
    The app every muxws command is dispatched into, built lazily from the current registrations
    and thrown away whenever they change.
    """
    global _dispatch_app
    if _dispatch_app is None:
        app = FastAPI()
        for registration in _registrations.values():
            # Before the viewset's own routes, not after: a trailing `/{pk}` route would otherwise
            # match `/{base_path}/schema` first and reject "schema" as a malformed key. This is
            # the same ordering `build_schema` uses on the REST side, for the same reason.
            _add_schema_route(app, registration)
            for kwargs in registration.route_kwargs:
                app.router.add_api_route(**kwargs)
        _dispatch_app = app
    return _dispatch_app


def _add_schema_route(app: FastAPI, registration: _Registration) -> None:
    """
    The muxws twin of the `/schema` endpoint `build_schema` puts on the REST router. It has to be
    built here rather than copied from there, because the REST one closes over the REST app and
    would report the wrong transport's endpoint set.
    """
    def schema() -> dict[str, Any]:
        return registration.schema()

    app.router.add_api_route(
        path=f"{registration.base_path.rstrip('/')}/schema",
        endpoint=schema,
        methods=["GET"],
        tags=registration.tags,
        description="Returns the OpenAPI schema for the ViewSet as reachable over muxws",
    )


def reset_registry() -> None:
    """Drops every registration. Exists for tests; calling it in an application is a mistake."""
    global _dispatch_app
    _registrations.clear()
    _dispatch_app = None
