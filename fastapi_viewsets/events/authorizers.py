"""
The functions that decide whether a caller may follow a topic, and the authorizers shipped with the
package. `settings.viewsets_event_authorizers` lists the ones an application uses:

    settings.viewsets_event_authorizers = [ViewsetTopics(), DeclaredSources()]
"""

import inspect

from collections.abc import Awaitable, Callable, Iterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_parameterless_sub_dependant, solve_dependencies
from starlette.routing import compile_path

from ..action_configuration import extra_middlewares_for, resolve_action_configuration
from ..conf import settings
from ..context import Context
from ..decorators.route_viewset import MIDDLEWARE_DEPENDS_ATTRIBUTE
from ..exceptions import EventTopicRefusedError
from ..middleware import Middleware
from .sources import declared_source
from .topics import is_viewset_topic, split_custom_topic

EventAuthorizer = Callable[[Request, Context, str], Awaitable[bool | None]]
"""
`await authorizer(request, context, topic)`: True admits the topic, False refuses it, None leaves
the decision to the next authorizer in `settings.viewsets_event_authorizers`. An `HTTPException` it
raises refuses the topic with that exception.
"""


async def authorize_topic(request: Request, context: Context, topic: str) -> None:
    """
    Runs `settings.viewsets_event_authorizers` in list order for `topic` and returns when one
    admits it. The first True admits the topic; the first False, or no decision at all, raises
    `EventTopicRefusedError` (403). An exception an authorizer raises, an `HTTPException` included,
    propagates and the authorizers after it do not run. An authorizer may also return its decision
    without an awaitable.
    """
    for authorizer in settings.viewsets_event_authorizers:
        decision = authorizer(request, context, topic)
        if inspect.isawaitable(decision):
            decision = await decision
        if decision is None:
            continue
        if decision:
            return
        break
    raise EventTopicRefusedError(topic)


async def allow_all(_request: Request, _context: Context, _topic: str) -> bool:
    """Admits every topic."""
    return True


async def allow_authenticated(_request: Request, context: Context, _topic: str) -> bool | None:
    """Admits a topic when `await context.user` is truthy; None otherwise, and for a context without
    `user`."""
    if "user" not in context:
        return None
    return True if await context.user else None


class DeclaredSources:
    """
    Admits a custom topic through the `authorizer` of the source that declared its name: the answer
    is that authorizer's. A viewset topic, a name no source declared, and a source declared without
    an authorizer get None.
    """

    async def __call__(self, request: Request, context: Context, topic: str) -> bool | None:
        parts = split_custom_topic(topic)
        if parts is None:
            return None
        declared = declared_source(parts[0])
        if declared is None or declared.authorizer is None:
            return None
        decision = declared.authorizer(request, context, topic)
        if inspect.isawaitable(decision):
            decision = await decision
        return decision

    def __repr__(self) -> str:
        return "DeclaredSources()"


def _routes(app: Any) -> Iterator[Any]:
    """Every route of `app` with every `include_router` prefix and dependency applied: its `path`,
    `endpoint` and `dependencies` are those the application serves the route with."""
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # a FastAPI whose include_router copies the routes, prefixed, onto the app
        yield from app.routes
        return
    yield from iter_route_contexts(app.routes)


def _gate_action(cls: type) -> str | None:
    """The action whose admission admits a caller to `cls`'s topic: `list_items`, else `retrieve`."""
    paths = getattr(cls, "__viewset_metadata__", {}).get("paths", {})
    for action in ("list_items", "retrieve"):
        if action in paths:
            return action
    return None


@dataclass(frozen=True)
class ViewsetMatch:
    """The viewset a topic belongs to, as `match_viewset` finds it."""

    viewset_class: type
    action: str
    """`list_items`, or `retrieve` for a class without `list_items`."""
    route: Any
    """The application's route of `action`."""
    path_params: dict[str, Any]
    """The values of the path parameters of the routed base path, read from the topic and converted
    as Starlette converts them for a request."""


def match_viewset(app: Any, topic: str) -> ViewsetMatch | None:
    """
    The first route of `app`, in routing order, whose endpoint carries `__viewset_class__` and, as
    `__viewset_action__`, the class's `list_items`, or `retrieve` for a class without `list_items`,
    and whose path without that action's path in `__viewset_metadata__["paths"]` matches `topic`
    as Starlette matches a request path, path parameters included: `/orgs/{org_id}/tracks`
    matches the topic `/orgs/7/tracks`. None when no route matches.
    """
    for route in _routes(app):
        endpoint = getattr(route, "endpoint", None)
        cls = getattr(endpoint, "__viewset_class__", None)
        path = getattr(route, "path", None)
        if cls is None or not isinstance(path, str):
            continue
        action = _gate_action(cls)
        if action is None or getattr(endpoint, "__viewset_action__", None) != action:
            continue
        relative = cls.__viewset_metadata__["paths"][action]
        if not path.endswith(relative):
            continue
        regex, _format, convertors = compile_path(path[: len(path) - len(relative)] if relative else path)
        matched = regex.match(topic)
        if matched is None:
            continue
        params = {name: convertors[name].convert(value) for name, value in matched.groupdict().items()}
        return ViewsetMatch(cls, action, route, params)
    return None


def find_viewset(app: Any, topic: str) -> tuple[type, str] | None:
    """`(viewset class, action)` of `match_viewset(app, topic)`, or None."""
    found = match_viewset(app, topic)
    return (found.viewset_class, found.action) if found is not None else None


def _with_path_params(request: Request, path_params: dict[str, Any]) -> Request:
    """`request`, or a copy of it whose `path_params` are `path_params` when there are any."""
    if not path_params:
        return request
    return Request({**request.scope, "path_params": path_params}, request.receive)


async def _route_dependencies_admit(request: Request, route: Any) -> bool:
    """
    Resolves the route's `dependencies` other than the one that runs `Middleware.depends()` (those of
    the application, of `include_router`, of the `APIRouter` and of the route) against `request`,
    as FastAPI resolves them before the endpoint, honouring `app.dependency_overrides`. False when a
    parameter of one does not validate; an exception one raises propagates.
    """
    dependencies = [
        depends
        for depends in getattr(route, "dependencies", None) or ()
        if not getattr(depends.dependency, MIDDLEWARE_DEPENDS_ATTRIBUTE, False)
    ]
    if not dependencies:
        return True
    path = getattr(route, "path_format", None) or route.path
    dependant = Dependant(
        path=path,
        dependencies=[get_parameterless_sub_dependant(depends=depends, path=path) for depends in dependencies],
    )
    accepted = inspect.signature(solve_dependencies).parameters
    async with AsyncExitStack() as stack:
        scope = {
            **request.scope,
            "fastapi_astack": stack,
            "fastapi_inner_astack": stack,
            "fastapi_function_astack": stack,
        }
        arguments: dict[str, Any] = {
            "request": Request(scope, request.receive),
            "dependant": dependant,
            "dependency_overrides_provider": getattr(route, "dependency_overrides_provider", None),
        }
        if "async_exit_stack" in accepted:
            arguments["async_exit_stack"] = stack
        if "embed_body_fields" in accepted:
            arguments["embed_body_fields"] = False
        solved = await solve_dependencies(**arguments)
    errors = solved.errors if hasattr(solved, "errors") else solved[1]
    return not errors


class ViewsetTopics:
    """
    Admits a viewset's topic to a caller the viewset itself admits to its `list_items`, or to its
    `retrieve` when it has no `list_items`.

    A topic that does not start with `/`, and a topic no viewset on `request.app` has, get None (see
    `match_viewset`).

    For a class without `@event_scope`, the checks a request to that action passes before the
    command chain run against the calling request, which carries the path parameters read from the
    topic as `path_params`:

    1. the route's `dependencies` other than `route_viewset`'s own, those of the application, of
       `include_router`, of the `APIRouter` and of the route itself; a parameter of one that does not
       validate refuses the topic (False);
    2. `depends()` of every effective middleware, with the viewset class and the calling context
       rebound to the action's `@action_configuration`, `Context(context.raw(),
       action_configuration=..., action_name=...)`: `settings.viewsets_command_middleware` followed
       by the middleware instances the configuration adds.

    A check in a middleware's `__call__` or in `perform_*` does not run for such a class. For a class
    with `@event_scope`, the gate run of `resolve_topic` admits the topic: the route's dependencies,
    the context processors, `depends()` and `__call__` of every effective middleware and the scope's
    resolver, against a request of its own (see `fastapi_viewsets.events.scopes`). A viewset that is
    not split by tenant gets the same checks with a constant resolver,
    `@event_scope(lambda request, context: "all")`.

    An `HTTPException` propagates: `Session` raises 401, `Authorization` 403, `RateLimiter` 429 and
    counts the call as one call of that action, `EventScopeMissingError` 403 for a resolver that
    answers None. When none raises, the topic is admitted.
    """

    async def __call__(self, request: Request, context: Context, topic: str) -> bool | None:
        from .scopes import resolve_topic, scope_of

        if not is_viewset_topic(topic):
            return None
        found = match_viewset(request.app, topic)
        if found is None:
            return None
        cls, action = found.viewset_class, found.action
        if scope_of(cls) is not None:
            await resolve_topic(request, topic)
            return True
        checked = _with_path_params(request, found.path_params)
        if not await _route_dependencies_admit(checked, found.route):
            return False
        action_configuration = resolve_action_configuration(cls, action)
        bound = Context(context.raw(), action_configuration=action_configuration, action_name=action)
        middlewares = settings.viewsets_command_middleware + extra_middlewares_for(
            action_configuration, settings.viewsets_command_middleware
        )
        for middleware in middlewares:
            if isinstance(middleware, Middleware):
                await middleware.depends(checked, cls, bound)
        return True

    def __repr__(self) -> str:
        return "ViewsetTopics()"
