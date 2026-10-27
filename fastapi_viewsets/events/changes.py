"""
`ChangeEvents`, the command middleware that publishes one `RecordsChanged` (`created`, `updated` or
`deleted`, with the affected pks) to a viewset's topic after each of its eight write actions, and
`write_topic`, the topic of a call for an endpoint that publishes itself.
"""

import inspect
import logging

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel

from ..action_configuration import resolve_action_configuration
from ..context import _Deferred, Context
from ..decorators.lifecycle_runner import execution_translator
from ..middleware import ActionExecution, Middleware, ViewSetResult
from .event import Event
from .scopes import EventScope, resolve_scope, SCOPE_CONTEXT_KEY, scope_of
from .sources import publish
from .topics import _validate_published_topic, scoped_topic, validate_topic

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)

TOPIC_CONTEXT_KEY = "fastapi_viewsets.events.topic"
"""The context key holding the routed base path of the call's viewset; it also reaches a `celery_viewset` worker."""

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
    """One record an action touched, by its pk; a frontend reads the record through the viewset's retrieve."""

    pk: Any


class RecordsChanged(Event):
    """The event `ChangeEvents` publishes to a viewset's topic, one per action."""

    event_name = "records_changed"

    op: Literal["created", "updated", "deleted"]
    changes: list[Change]


def routed_base_path(request: "Request") -> str | None:
    """
    The routed base path of the viewset `request` was routed to, with path parameters filled in:
    `/api/music` for `/api/music/3`, `/orgs/7/tracks` for a viewset at `/orgs/{org_id}/tracks`. It is
    the path below `root_path` minus the action's relative path segments. None for a route that
    `route_viewset` did not register.
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
    Publishes one `RecordsChanged` after each successful write action of a viewset. `__call__` stores
    the routed base path (and, for an `@event_scope` class, the deferred scope key) in the context in
    the web process; `execute()` publishes where the action executes. The topic and pk field are
    resolved before the action runs, so a missing one raises first; a failing publish propagates after
    it has run. Configured per viewset or action with `{ChangeEvents: False | {"topic": ...}}`, or with
    a `ChangeEvents()` instance as the key. `topic_for` and `changes_for` are the subclass hooks.
    See docs/guide/events.md.
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
            if scope is not None and self._carries_scope(context):
                context.set(SCOPE_CONTEXT_KEY, _Deferred(lambda: _scope_entry(scope, request, context)))
        return await call_next()

    def _carries_scope(self, context: Context) -> bool:
        """Whether `__call__` stores the scope: not for a write action configured `False` or with a `topic`."""
        if context.action_name not in _OPS:
            return True
        options = self.options_for(context)
        return options is not None and "topic" not in options

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
        """Whether other `ChangeEvents` instances, configured as keys for the action, publish instead of this one."""
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
        """The topic `execute()` publishes to; None for a scope key of None."""
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
        The topic of an execution, and the hook a subclass overrides: the configured `topic` (a
        callable is called with `execution`); else, for a top-level execution, the context's
        `TOPIC_CONTEXT_KEY`; else `__viewset_metadata__["base_path"]`. Raises `TypeError` with none.
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
        The record's pk field: `__viewset_metadata__["pk_field_name"]`, else the viewset's `pk_field_name`,
        else its `pk_field`. Raises `TypeError` with none of the three.
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
        """One change per pk of the `pk` argument for `deleted`, else per returned record (`ViewSetResult.body`)."""
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


_NOT_CARRIED = object()


async def _carried_topic(scope: EventScope, context: Context) -> Any:
    """The store topic of the context's `SCOPE_CONTEXT_KEY` entry for `scope` (None for None), else `_NOT_CARRIED`."""
    raw = context.raw()
    if SCOPE_CONTEXT_KEY not in raw:
        return _NOT_CARRIED
    carried = raw[SCOPE_CONTEXT_KEY]
    if isinstance(carried, _Deferred):
        carried = await carried
    if carried is None:
        return None
    if isinstance(carried, dict) and carried.get("name") == scope.name and isinstance(carried.get("key"), str):
        return scoped_topic(scope.name, carried["key"])
    return _NOT_CARRIED


async def _scoped_write_topic(
    scope: EventScope, request: "Request | None", context: Context, carries: bool
) -> str | None:
    """The store topic of a scoped write: the carried entry when `carries`, else the resolver's key; None for None."""
    if carries:
        carried = await _carried_topic(scope, context)
        if carried is not _NOT_CARRIED:
            return carried
    key = await resolve_scope(scope, request, context)
    return None if key is None else scoped_topic(scope.name, key)


async def write_topic(viewset: Any, request: "Request | None", context: Context) -> str | None:
    """
    The topic a write of the current call of `viewset` (class or instance) publishes to, for an
    endpoint that writes outside the eight actions. For an `@event_scope` class `@{name}:{key}` (None
    for a None key; resolver exceptions propagate; a `celery_viewset` worker only uses the key carried
    from the web process). Otherwise the routed base path, else `__viewset_metadata__["base_path"]`.
    """
    cls = viewset if isinstance(viewset, type) else type(viewset)
    scope = scope_of(cls)
    if scope is not None:
        if execution_translator.get() is not None:
            carried = await _carried_topic(scope, context)
            return None if carried is _NOT_CARRIED else carried
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
