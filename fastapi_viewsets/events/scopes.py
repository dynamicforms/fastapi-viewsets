"""
Event scopes: a viewset's events are stored under `@{scope.name}:{key}`, the key answered by the scope's
resolver from the request (in a gate run on subscribe/poll, in the writing call on a write).

The resolver is the scope's authorization point: it answers a key only for a scope the caller may read,
and must derive it from values a check in the gate run validated, never from raw client input.
"""

import inspect

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING, TypeVar

from fastapi import Request

from ..action_configuration import extra_middlewares_for, resolve_action_configuration
from ..conf import settings
from ..context import build_context, Context
from ..exceptions import EventScopeMissingError, EventTopicRefusedError
from ..middleware import Middleware, run_command_chain, ViewSetResult
from ..routing import GATE_RUN_SCOPE_KEY
from .topics import _scope_key_text, _scope_name, is_viewset_topic, scoped_topic

if TYPE_CHECKING:
    from .authorizers import ViewsetMatch

T = TypeVar("T")

ScopeKey = str | int
"""A scope key: a `str` without whitespace or control characters, or an `int`."""

ScopeResolver = Callable[["Request | None", Context], "ScopeKey | None | Awaitable[ScopeKey | None]"]
"""`resolver(request, context)`, sync or async: the caller's scope key, or None. An `HTTPException` refuses."""

SCOPE_CONTEXT_KEY = "fastapi_viewsets.events.scope"
"""Context key where `ChangeEvents.__call__` stores the call's scope: an awaitable answering
`{"name", "key"}` or None. A `celery_viewset` worker receives the resolved value and never calls the resolver."""

_GATE_CACHE_SCOPE_KEY = "fastapi_viewsets.events.gate_cache"

__all__ = [
    "GATE_RUN_SCOPE_KEY",
    "SCOPE_CONTEXT_KEY",
    "EventScope",
    "ResolvedTopic",
    "ScopeKey",
    "ScopeResolver",
    "event_scope",
    "reset_event_scopes",
    "resolve_scope",
    "resolve_topic",
    "scope_of",
]


@dataclass(frozen=True)
class EventScope:
    """The scope a viewset class's events are stored and delivered under."""

    name: str
    """The scope's name: the first part of its store topics. No whitespace, control character or `:`."""
    resolver: ScopeResolver

    def topic(self, key: ScopeKey) -> str:
        """The store topic `@{name}:{key}`; raises as `scoped_topic` does."""
        return scoped_topic(self.name, key)


# scope name -> the class that declared it
_names: dict[str, type] = {}

# scope name -> further classes sharing it through a sub_viewset mount relation
_sharers: dict[str, list[type]] = {}


def event_scope(resolver: ScopeResolver, *, name: str | None = None) -> Callable[[type[T]], type[T]]:
    """
    Declare the scope of a viewset class's events (`cls.__event_scope__`); `name` defaults to the
    class's dotted path. Besides the first declaring class, only its subclasses, a reloaded copy of it,
    and classes mount-related to it via `sub_viewset` may declare the same name.

    Raises `ValueError` for any other class and for an invalid name (empty, whitespace, control
    characters, `:`). Classes defined in a function need an explicit `name` each.
    """

    def decorator(cls: type[T]) -> type[T]:
        scope_name = _scope_name(name if name is not None else f"{cls.__module__}.{cls.__qualname__}")
        holder = _names.get(scope_name)
        if holder is None or _reloaded(cls, holder):
            _names[scope_name] = cls
        elif cls is not holder and not issubclass(cls, holder):
            sharers = _sharers.setdefault(scope_name, [])
            if any(issubclass(cls, other) for other in sharers):
                pass  # a subclass of a sharer shares the name as the sharer does
            elif any(_mount_related(cls, other) for other in (holder, *sharers)):
                sharers.append(cls)
            else:
                hint = "; a class defined in a function needs event_scope(name=...)" if _is_local(cls) else ""
                raise ValueError(
                    f"the event scope {scope_name!r} is declared by {holder.__module__}.{holder.__qualname__}; "
                    f"{cls.__module__}.{cls.__qualname__} is not a subclass of it, nor mounted under or "
                    f"mounting a class that declares it{hint}"
                )
        cls.__event_scope__ = EventScope(scope_name, resolver)
        return cls

    return decorator


def _mount_related(cls: type, other: type) -> bool:
    """Whether one of the two is mounted under the other, at any depth."""
    from ..decorators.sub_viewsets import mounted_under

    return other in set(mounted_under(cls)) or cls in set(mounted_under(other))


def _is_local(cls: type) -> bool:
    return "<locals>" in cls.__qualname__.split(".")


def _reloaded(cls: type, holder: type) -> bool:
    """Whether `cls` is `holder`'s replacement from a reloaded module (same dotted path, not function-local)."""
    return (
        cls is not holder
        and (cls.__module__, cls.__qualname__) == (holder.__module__, holder.__qualname__)
        and not _is_local(cls)
    )


def scope_of(cls: type) -> EventScope | None:
    """The `EventScope` `cls` carries or inherits; None for a class without `@event_scope`."""
    scope = getattr(cls, "__event_scope__", None)
    return scope if isinstance(scope, EventScope) else None


def reset_event_scopes() -> None:
    """Forgets the scope names declared in this process; the classes keep their `__event_scope__`."""
    _names.clear()
    _sharers.clear()


async def resolve_scope(scope: EventScope, request: "Request | None", context: Context) -> str | None:
    """
    Call the resolver and return the key as text (None stays None). Raises `ValueError` for an invalid
    `str` key, `TypeError` for a type other than `str`/`int` (`bool` included).
    """
    key = scope.resolver(request, context)
    if inspect.isawaitable(key):
        key = await key
    if key is None:
        return None
    return _scope_key_text(key)


@dataclass(frozen=True)
class ResolvedTopic:
    """A client topic with the store topic the server keeps its events under."""

    topic: str
    """The client topic: what the client subscribes to, polls and receives in `:topic`."""
    store_topic: str
    """The topic the events are published, stored and delivered under; `topic` for an unscoped topic."""
    scope: str | None
    """The scope key as text; None for an unscoped topic."""


async def resolve_topic(request: Request, topic: str) -> ResolvedTopic:
    """
    The store topic of client topic `topic` for the caller of `request`; unscoped topics map to themselves.
    For a scoped class the gate run answers the key; it raises `EventTopicRefusedError` or
    `EventScopeMissingError` (both 403), or what a check raised. Results are cached in `request.scope`.
    """
    from .authorizers import match_viewset

    if not is_viewset_topic(topic):
        return ResolvedTopic(topic, topic, None)
    found = match_viewset(request.app, topic)
    scope = scope_of(found.viewset_class) if found is not None else None
    if found is None or scope is None:
        return ResolvedTopic(topic, topic, None)
    cache: dict[Any, Any] = request.scope.setdefault(_GATE_CACHE_SCOPE_KEY, {})
    route_path = getattr(found.route, "path_format", None) or found.route.path
    cache_key = (found.viewset_class, found.action, route_path, tuple(sorted(found.path_params.items())))
    if cache_key in cache:
        cached = cache[cache_key]
        if isinstance(cached, BaseException):
            raise cached
        store_topic, key = cached
        return ResolvedTopic(topic, store_topic, key)
    try:
        key = await _gate_run(request, topic, found, scope)
    except Exception as error:
        cache[cache_key] = error
        raise
    store_topic = scoped_topic(scope.name, key)
    cache[cache_key] = (store_topic, key)
    return ResolvedTopic(topic, store_topic, key)


async def _empty_body() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _starlette_route(route: Any) -> Any:
    """The route Starlette would put in `scope["route"]`: the one a `RouteContext` wraps, else `route`."""
    try:
        from fastapi.routing import RouteContext
    except ImportError:  # a FastAPI whose include_router copies the routes onto the app
        return route
    return route.route if isinstance(route, RouteContext) else route


def _gate_request(request: Request, topic: str, found: "ViewsetMatch") -> Request:
    root_path = request.scope.get("root_path", "")
    headers = [
        (name, value)
        for name, value in request.scope["headers"]
        if name not in (b"idempotency-key", b"content-length", b"content-type")
    ]
    state = {name: value for name, value in (request.scope.get("state") or {}).items() if name != "_context"}
    gate_scope = {name: value for name, value in request.scope.items() if name != _GATE_CACHE_SCOPE_KEY}
    gate_scope.update(
        {
            "method": "GET",
            "path": root_path + topic,
            "raw_path": (root_path + topic).encode(),
            "query_string": b"",
            "headers": headers,
            "path_params": found.path_params,
            "route": _starlette_route(found.route),
            "endpoint": getattr(found.route, "endpoint", None),
            "state": state,
            GATE_RUN_SCOPE_KEY: found.action,
        }
    )
    return Request(gate_scope, receive=_empty_body)


async def _gate_run(request: Request, topic: str, found: "ViewsetMatch", scope: EventScope) -> str:
    """
    Run everything a `GET` of the gate action runs before the action (route dependencies, context
    processors, middleware `depends()` and `__call__`) on a copy of the caller's request, with the
    scope's resolver in place of the action. Returns the key.
    """
    from .authorizers import _route_dependencies_admit

    gate_request = _gate_request(request, topic, found)
    if not await _route_dependencies_admit(gate_request, found.route):
        raise EventTopicRefusedError(topic)
    cls, action = found.viewset_class, found.action
    action_configuration = resolve_action_configuration(cls, action)
    context = await build_context(gate_request, cls, action_configuration, action)
    middlewares = settings.viewsets_command_middleware + extra_middlewares_for(
        action_configuration, settings.viewsets_command_middleware
    )
    for middleware in middlewares:
        if isinstance(middleware, Middleware):
            await middleware.depends(gate_request, cls, context)

    reached = False
    key: str | None = None

    async def terminal() -> ViewSetResult:
        nonlocal reached, key
        reached = True
        key = await resolve_scope(scope, gate_request, context)
        return ViewSetResult(body=None)

    result = await run_command_chain(middlewares, gate_request, cls, context, terminal)
    if not reached or (result.status_code or 200) >= 400:
        raise EventTopicRefusedError(topic)
    if key is None:
        raise EventScopeMissingError(topic)
    return key
