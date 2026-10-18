"""
Event scopes: the store topic of a viewset's events, derived by the server from the request.

    async def org_scope(request, context):
        # set by a middleware that verified the caller's membership of the organisation
        return await context.org_id if "org_id" in context else None

    @event_scope(org_scope)
    @route_viewset(router, "/tags")
    class TagsViewSet(ViewSetMixin[int, Tag]): ...

A client subscribes to and polls a viewset's routed base path, as for any viewset topic. For a class
that carries `@event_scope` the server stores and delivers the topic's events under the store topic
`@{scope.name}:{key}`, where `key` is what the scope's resolver answers: on a subscribe and a poll,
in a gate run of the subscribing request (everything a `GET` of the viewset's gate action by that
caller runs before the action executes); on a write, in the call that writes. Every routed path of the
class and of its subclasses whose resolver answers the same key shares one store topic, and a
subscriber of another key never receives its events.

The resolver is the scope's authorization point: it answers a key only for a scope the caller may
read, and raises `HTTPException` or answers None otherwise. It reads a value a check in the gate run
established (a middleware's `depends()` or `__call__`, a route dependency, a validating context
processor), or the result of the viewset's own read check called with the context; never a client
value no check in the gate run validated.
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
"""A scope's key as a resolver answers it: a `str` without whitespace or control characters, or an
`int`."""

ScopeResolver = Callable[["Request | None", Context], "ScopeKey | None | Awaitable[ScopeKey | None]"]
"""
`resolver(request, context)`, sync or async: the key of the scope the caller may read, or None for a
caller without one. An `HTTPException` it raises refuses the caller with that exception.
"""

SCOPE_CONTEXT_KEY = "fastapi_viewsets.events.scope"
"""The context key under which `ChangeEvents.__call__` stores `{"name": str, "key": str}` for a write
call of a class with `@event_scope`, in the web process. The worker of a `celery_viewset` action
reads the key from the context it is handed."""

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


def event_scope(resolver: ScopeResolver, *, name: str | None = None) -> Callable[[type[T]], type[T]]:
    """
    Declares the scope of a viewset class's events: sets `cls.__event_scope__` to
    `EventScope(name, resolver)`, `name` defaulting to `f"{cls.__module__}.{cls.__qualname__}"`.
    Subclasses inherit the attribute, and with it the name. The decorator may be applied before or
    after `route_viewset`.

    Raises `ValueError` for a name that is empty or holds whitespace, a control character or `:`,
    and for a name another class already declared, unless the class is that class, a subclass of
    it, or a class of the same module and qualified name (a reloaded module), which takes the name
    over.
    """

    def decorator(cls: type[T]) -> type[T]:
        scope_name = _scope_name(name if name is not None else f"{cls.__module__}.{cls.__qualname__}")
        holder = _names.get(scope_name)
        if holder is None or (cls.__module__, cls.__qualname__) == (holder.__module__, holder.__qualname__):
            _names[scope_name] = cls
        elif cls is not holder and not issubclass(cls, holder):
            raise ValueError(
                f"the event scope {scope_name!r} is declared by {holder.__module__}.{holder.__qualname__}; "
                f"{cls.__module__}.{cls.__qualname__} is not a subclass of it"
            )
        cls.__event_scope__ = EventScope(scope_name, resolver)
        return cls

    return decorator


def scope_of(cls: type) -> EventScope | None:
    """The `EventScope` `cls` carries or inherits; None for a class without `@event_scope`."""
    scope = getattr(cls, "__event_scope__", None)
    return scope if isinstance(scope, EventScope) else None


def reset_event_scopes() -> None:
    """Forgets the scope names declared in this process; the classes keep their `__event_scope__`."""
    _names.clear()


async def resolve_scope(scope: EventScope, request: "Request | None", context: Context) -> str | None:
    """
    Calls the scope's resolver, awaiting an awaitable answer, and returns the key as text: None
    stays None, an `int` (not a `bool`) becomes `str(key)`. Raises `ValueError` for a `str` that is
    empty or holds whitespace or a control character, and `TypeError` for an answer of any other
    type, `bool` included. An exception the resolver raises propagates.
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
    The store topic of the client topic `topic` for the caller of `request`.

    A topic that is not a viewset topic, a viewset topic no viewset on `request.app` has, and the
    topic of a class without `@event_scope` are their own store topic. For a class with a scope the
    gate run of `request` for that topic answers the key, and the store topic is `@{name}:{key}`.
    The gate run raises what it refuses with: `EventTopicRefusedError` (403) when a route dependency
    does not validate or the command chain does not reach the action or answers a status of 400 or
    above, `EventScopeMissingError` (403) when the resolver answers None, and the exception a
    check or the resolver raised.

    One gate run answers every topic of one request with the same viewset class, gate action, route
    and path parameter values; its answer, or its exception, is kept in `request.scope`.
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
    """The route Starlette puts in `scope["route"]` for a request routed to `route`: the route a
    `RouteContext` wraps, else `route` itself."""
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
    Runs what a `GET` of the gate action by the caller of `request` runs before the action executes:
    the route's dependencies, then the context processors, `depends()` and `__call__` of every
    effective middleware with the viewset class as `viewset`, against a request of its own (the
    caller's headers, cookies, client, transport, peer and lifespan state; the topic's path and path
    parameters; a context of its own) whose scope carries `GATE_RUN_SCOPE_KEY`. In place of the
    action the scope's resolver runs and answers the key. The result's headers, cookies and body are
    discarded.
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
