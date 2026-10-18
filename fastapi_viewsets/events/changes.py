"""
`ChangeEvents`, the command middleware that publishes one `RecordsChanged` to a viewset's topic after
each of its create, update and destroy actions.

    settings.viewsets_command_middleware = [Idempotency(...), ChangeEvents(), Session(), ...]

`write_topic` answers the topic of a call for an endpoint that publishes itself.

| `execution.action` | `op` | `changes` |
|---|---|---|
| `create` | `created` | the pk of the returned record |
| `bulk_create` | `created` | the pk of each returned record |
| `update`, `partial_update` | `updated` | the pk of the returned record |
| `bulk_update`, `bulk_partial_update` | `updated` | the pk of each returned record |
| `destroy` | `deleted` | the `pk` argument |
| `bulk_destroy` | `deleted` | each pk of the `pk` argument |
"""

import inspect
import logging

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel

from ..action_configuration import resolve_action_configuration
from ..context import _Deferred, Context
from ..middleware import ActionExecution, Middleware, ViewSetResult
from .event import Event
from .scopes import EventScope, resolve_scope, SCOPE_CONTEXT_KEY, scope_of
from .sources import publish
from .topics import _validate_published_topic, scoped_topic, validate_topic

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

TOPIC_CONTEXT_KEY = "fastapi_viewsets.events.topic"
"""The context key under which `ChangeEvents.__call__` stores the routed base path of the call's
viewset. The worker of a `celery_viewset` action a request called reads the topic from the context
it is handed."""

_OPS: dict[str, Literal["created", "updated", "deleted"]] = {
    "create": "created",
    "bulk_create": "created",
    "update": "updated",
    "partial_update": "updated",
    "bulk_update": "updated",
    "bulk_partial_update": "updated",
    "destroy": "deleted",
    "bulk_destroy": "deleted",
}

_BULK_ACTIONS = frozenset({"bulk_create", "bulk_update", "bulk_partial_update", "bulk_destroy"})

_OPTIONS = frozenset({"topic"})


class Change(BaseModel):
    """One record an action touched, by its pk. A frontend reads the record through the viewset's
    retrieve, which answers in the endpoint's format and in the reader's context."""

    pk: Any


class RecordsChanged(Event):
    """The event `ChangeEvents` publishes to a viewset's topic, one per action."""

    event_name = "records_changed"

    op: Literal["created", "updated", "deleted"]
    changes: list[Change]


def routed_base_path(request: "Request") -> str | None:
    """
    The routed base path of the viewset whose endpoint `request` was routed to: the `include_router`
    prefixes followed by `base_path`, e.g. `/api/music` for a request to `/api/music/3`, with the
    values of the request's path parameters in place, e.g. `/orgs/7/tracks` for a viewset at
    `/orgs/{org_id}/tracks`.

    It is the request's path, relative to the application's `root_path`, with as many trailing
    segments removed as the action's relative path (`__viewset_metadata__["paths"][action]`) has.
    Each path parameter of that relative path matches one segment. None when the matched route is
    not an endpoint `route_viewset` registered.
    """
    scope = request.scope
    endpoint = getattr(scope.get("route"), "endpoint", None)
    metadata = getattr(getattr(endpoint, "__viewset_class__", None), "__viewset_metadata__", None)
    if metadata is None:
        return None
    relative = metadata["paths"].get(getattr(endpoint, "__viewset_action__", None))
    if relative is None:
        return None
    path = _path_below_root(scope)
    segments = relative.count("/")
    return path.rsplit("/", segments)[0] if segments else path


def _path_below_root(scope: Mapping[str, Any]) -> str:
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path) and path[len(root_path) : len(root_path) + 1] in ("", "/"):
        return path[len(root_path) :]
    return path


class ChangeEvents(Middleware):
    """
    Publishes one `RecordsChanged` after each of the eight write actions of a viewset (see the
    module's table). An action that raises publishes nothing; any other action publishes nothing.

    `__call__` runs in the web process and stores `routed_base_path(request)` in the context under
    `TOPIC_CONTEXT_KEY`, on every action. For a write action of a class with `@event_scope` (see
    `fastapi_viewsets.events.scopes`) it also stores under `SCOPE_CONTEXT_KEY` the deferred
    resolution of the scope's key with the call's request and context. The resolver runs after the
    whole command chain, wherever `ChangeEvents` sits in it: in `execute()` for an action that
    executes in the web process, before the dispatch for a `celery_viewset` action, which then
    carries `{"name": ..., "key": ...}`, or None for a key of None, to the worker. An exception of
    the resolver refuses the call before the action. `execute()` runs where the action executes and
    publishes after `call_next()` returns. An exception of the publish,
    `store.append` failing included, propagates from `execute()` after the action has run.

    The topic `execute()` publishes to:

    1. for a class without `@event_scope`, `topic_for(execution)`;
    2. for a class with `@event_scope`, `topic_for(execution)` of a subclass that overrides it;
       else, for an execution a request called, `@{name}:{key}` of the key under
       `SCOPE_CONTEXT_KEY` (resolved there in the web process); else `@{name}:{key}` of the key the
       resolver answers in `execute()`, with the execution's request and context. A key of None
       publishes nothing and logs a warning. A `topic` option on a class with `@event_scope` raises `TypeError`.

    `@action_configuration` on a class or a `perform_*` method:

    - `{ChangeEvents: False}`: no event for that viewset or action;
    - `{ChangeEvents: {"topic": ...}}`: a `str`, or a `Callable[[ActionExecution], str]`, the topic
      to publish to instead of the routed base path;
    - `{ChangeEvents(): {...}}`: a fresh instance as the key adds the plugin to that viewset or
      action alone; the instance reads the value under its own key. While the configuration has a
      `ChangeEvents` instance as a key, a `ChangeEvents` that is not one of its keys, such as the
      one in `settings.viewsets_command_middleware`, publishes nothing.

    `True` and no value are the defaults. Any other value, or a dict with a key other than `topic`,
    raises `TypeError`.

    `execute()` resolves the topic, and for a `created` or `updated` op the pk field, before
    `call_next()`: a missing or malformed one raises before the action runs. `topic_for` and
    `changes_for` are the hooks for a subclass.
    """

    async def __call__(
        self,
        request: "Request | None",
        viewset: Any,
        context: Context,
        call_next: Callable[[], Awaitable[ViewSetResult]],
    ) -> ViewSetResult:
        if request is not None:
            topic = routed_base_path(request)
            if topic is not None:
                context.set(TOPIC_CONTEXT_KEY, topic)
            cls = viewset if isinstance(viewset, type) else type(viewset)
            scope = scope_of(cls)
            if scope is not None and context._action_name in _OPS:
                options = self.options_for(context)
                if options is not None and "topic" not in options:
                    context.set(SCOPE_CONTEXT_KEY, _Deferred(lambda: _scope_entry(scope, request, context)))
        return await call_next()

    def config_from(self, context: Context) -> Any:
        """The value configured under this instance as the key, else under its class."""
        own = context.configuration_for(self)
        return own if own is not None else context.configuration_for(type(self))

    def options_for(self, context: Context) -> dict[str, Any] | None:
        """The configured options as a dict; None when the plugin is disabled for the call."""
        config = self.config_from(context)
        if config is None or config is True:
            return {}
        if config is False:
            return None
        if not isinstance(config, dict):
            raise TypeError(
                f"{type(self).__name__} is configured with {config!r}; it takes False, True or a dict with "
                f"the key 'topic'"
            )
        unknown = set(config) - _OPTIONS
        if unknown:
            raise TypeError(
                f"{type(self).__name__} is configured with unknown option(s) {', '.join(sorted(map(repr, unknown)))}; "
                f"it takes 'topic' only"
            )
        return config

    def replaced_for(self, execution: ActionExecution) -> bool:
        """Whether the execution's action configuration has `ChangeEvents` instances as keys and this
        instance is not one of them; those instances then publish for the execution."""
        injected = [
            key
            for key in resolve_action_configuration(execution.viewset_class, execution.action)
            if isinstance(key, ChangeEvents)
        ]
        return bool(injected) and not any(key is self for key in injected)

    async def execute(self, execution: ActionExecution, call_next: Callable[[], Awaitable[Any]]) -> Any:
        op = _OPS.get(execution.action)
        options = self.options_for(execution.context) if op is not None else None
        if options is None or self.replaced_for(execution):
            return await call_next()
        topic = await self._resolve_topic(execution)
        if topic is None:
            scope = scope_of(execution.viewset_class)
            logger.warning(
                "events: %s.%s: the event scope %r answered no key; no %s is published",
                execution.viewset_class.__qualname__,
                execution.action,
                scope.name if scope is not None else None,
                RecordsChanged.event_name,
            )
            return await call_next()
        if op != "deleted":
            self.pk_field_for(execution)
        result = await call_next()
        await publish(topic, RecordsChanged(op=op, changes=self.changes_for(execution, op, result)))
        return result

    async def _resolve_topic(self, execution: ActionExecution) -> str | None:
        """The topic `execute()` publishes to (see the class's topic order); None for a scope key of
        None."""
        scope = scope_of(execution.viewset_class)
        if scope is None:
            return validate_topic(self.topic_for(execution))
        if "topic" in (self.options_for(execution.context) or {}):
            raise TypeError(
                f"{execution.viewset_class.__qualname__}: event_scope and the {type(self).__name__} 'topic' "
                f"option exclude each other"
            )
        if type(self).topic_for is not ChangeEvents.topic_for:
            return _validate_published_topic(self.topic_for(execution))
        return await _scoped_write_topic(scope, execution.request, execution.context, execution.parent is None)

    def topic_for(self, execution: ActionExecution) -> str:
        """
        The topic of an execution of a class without `@event_scope`, and the hook a subclass
        overrides, for any class: the configured `topic` (a callable is called with `execution`);
        else, for an execution a request called (`execution.parent` None), the routed base path in
        the context under `TOPIC_CONTEXT_KEY`; else `__viewset_metadata__["base_path"]`, for an
        execution dispatched outside any call or in the execution of another action, whose context
        carries the topic of that other action. Raises `TypeError` when there is none of the three.
        """
        topic = (self.options_for(execution.context) or {}).get("topic")
        if callable(topic):
            return topic(execution)
        if topic is not None:
            return topic
        routed = execution.context.raw().get(TOPIC_CONTEXT_KEY) if execution.parent is None else None
        if routed is not None:
            return routed
        metadata = getattr(execution.viewset_class, "__viewset_metadata__", None)
        if metadata is not None:
            return metadata["base_path"]
        routed_note = (
            f"the context has no {TOPIC_CONTEXT_KEY!r}"
            if execution.parent is None
            else f"the execution was dispatched in {execution.parent.viewset_class}.{execution.parent.action}, "
            f"whose topic is not this viewset's"
        )
        raise TypeError(
            f"{execution.viewset_class.__qualname__}.{execution.action}: {type(self).__name__} has no topic: "
            f"no 'topic' is configured, {routed_note}, and the viewset has no __viewset_metadata__ from "
            f"route_viewset"
        )

    def pk_field_for(self, execution: ActionExecution) -> str:
        """
        The name of the field that holds a record's pk: `__viewset_metadata__["pk_field_name"]`, else
        the viewset's `pk_field_name` attribute, else its `pk_field` attribute. Raises `TypeError`
        naming the viewset when there is none of the three.
        """
        metadata = getattr(execution.viewset_class, "__viewset_metadata__", None) or {}
        name = metadata.get("pk_field_name")
        if name is None:
            name = getattr(execution.viewset, "pk_field_name", None)
        if name is None:
            name = getattr(execution.viewset, "pk_field", None)
        if not isinstance(name, str):
            raise TypeError(
                f"{execution.viewset_class.__qualname__} names no pk field for {type(self).__name__}: pass "
                f"route_viewset(pk_field_name=...), or set pk_field_name or pk_field on the viewset"
            )
        return name

    def changes_for(self, execution: ActionExecution, op: str, result: Any) -> list[Change]:
        """
        The changes of one execution: for `deleted`, one per pk of the `pk` argument; otherwise one per
        returned record, its pk read from the field `pk_field_for(execution)` names. A `ViewSetResult`
        contributes its `body`.
        """
        bulk = execution.action in _BULK_ACTIONS
        if op == "deleted":
            pk = _argument(execution, "pk")
            return [Change(pk=one) for one in (pk if bulk else [pk])]
        body = result.body if isinstance(result, ViewSetResult) else result
        field = self.pk_field_for(execution)
        return [Change(pk=_read_pk(execution, record, field)) for record in (body if bulk else [body])]


async def _scope_entry(scope: EventScope, request: "Request | None", context: Context) -> dict[str, str] | None:
    key = await resolve_scope(scope, request, context)
    return None if key is None else {"name": scope.name, "key": key}


async def _scoped_write_topic(
    scope: EventScope, request: "Request | None", context: Context, carries: bool
) -> str | None:
    """The store topic of a write of a class with `scope`: from the entry under `SCOPE_CONTEXT_KEY`
    when `carries` and the context holds one of this scope, else from the key the resolver answers
    with `request` and `context`; None for a key of None."""
    carried = context.raw().get(SCOPE_CONTEXT_KEY) if carries else None
    if isinstance(carried, _Deferred):
        carried = await carried
        if carried is None:
            return None
    if isinstance(carried, dict) and carried.get("name") == scope.name and isinstance(carried.get("key"), str):
        return scoped_topic(scope.name, carried["key"])
    key = await resolve_scope(scope, request, context)
    return None if key is None else scoped_topic(scope.name, key)


async def write_topic(viewset: Any, request: "Request | None", context: Context) -> str | None:
    """
    The topic a write of the current call of `viewset` (a viewset class or instance) publishes to,
    for an endpoint that writes outside the eight actions and publishes itself. For a class with
    `@event_scope`, the store topic `@{name}:{key}` of the key the scope's resolver answers with
    `request` and `context` (the carried key of a write action, see `SCOPE_CONTEXT_KEY`), None for a
    key of None; an exception of the resolver propagates. For a class without it, the topic under
    `TOPIC_CONTEXT_KEY`, else `__viewset_metadata__["base_path"]`, else None.
    """
    cls = viewset if isinstance(viewset, type) else type(viewset)
    scope = scope_of(cls)
    if scope is not None:
        return await _scoped_write_topic(scope, request, context, True)
    routed = context.raw().get(TOPIC_CONTEXT_KEY)
    if routed is not None:
        return routed
    metadata = getattr(cls, "__viewset_metadata__", None)
    return metadata["base_path"] if metadata is not None else None


def _argument(execution: ActionExecution, name: str) -> Any:
    """The argument `name` of the action's call, passed by keyword or by position."""
    if name in execution.kwargs:
        return execution.kwargs[name]
    method = getattr(execution.viewset_class, execution.action)
    bound = inspect.signature(method).bind_partial(execution.viewset, *execution.args, **execution.kwargs)
    return bound.arguments[name]


def _read_pk(execution: ActionExecution, record: Any, field: str) -> Any:
    if isinstance(record, Mapping):
        if field in record:
            return record[field]
    elif hasattr(record, field):
        return getattr(record, field)
    raise TypeError(
        f"{execution.viewset_class.__qualname__}.{execution.action} returned a record without the pk field "
        f"{field!r}: {record!r}"
    )
