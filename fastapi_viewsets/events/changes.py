"""
`ChangeEvents`, the command middleware that publishes one `RecordsChanged` to a viewset's topic after
each of its create, update and destroy actions.

    settings.viewsets_command_middleware = [Idempotency(...), ChangeEvents(), Session(), ...]

| `execution.action` | `op` | `changes` |
|---|---|---|
| `create` | `created` | the returned record |
| `bulk_create` | `created` | each returned record |
| `update`, `partial_update` | `updated` | the returned record |
| `bulk_update`, `bulk_partial_update` | `updated` | each returned record |
| `destroy` | `deleted` | the `pk` argument |
| `bulk_destroy` | `deleted` | each pk of the `pk` argument |
"""

import inspect

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from ..action_configuration import resolve_action_configuration
from ..context import Context
from ..middleware import ActionExecution, Middleware, ViewSetResult
from .event import Event
from .sources import publish
from .topics import validate_topic

if TYPE_CHECKING:
    from fastapi import Request

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

_OPTIONS = frozenset({"record", "topic"})


class Change(BaseModel):
    """One record an action touched: its pk, and with `{"record": True}` the record itself."""

    pk: Any
    record: dict[str, Any] | None = None


class RecordsChanged(Event):
    """The event `ChangeEvents` publishes to a viewset's topic, one per action."""

    event_name = "records_changed"

    op: Literal["created", "updated", "deleted"]
    changes: list[Change]


def routed_base_path(request: "Request") -> str | None:
    """
    The routed base path of the viewset whose endpoint `request` was routed to: the `include_router`
    prefixes followed by `base_path`, e.g. `/api/music` for a request to `/api/music/3`, with the
    values of the request's path parameters in place, e.g. `/clubs/7/tracks` for a viewset at
    `/clubs/{club_id}/tracks`.

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
    `TOPIC_CONTEXT_KEY`, on every action. `execute()` runs where the action executes and publishes
    after `call_next()` returns. An exception of the publish, `store.append` failing included,
    propagates from `execute()` after the action has run.

    `@action_configuration` on a class or a `perform_*` method:

    - `{ChangeEvents: False}`: no event for that viewset or action;
    - `{ChangeEvents: {"record": True}}`: `created` and `updated` changes carry the returned record
      as `model_dump(mode="json")`;
    - `{ChangeEvents: {"topic": ...}}`: a `str`, or a `Callable[[ActionExecution], str]`, the topic
      to publish to instead of the routed base path;
    - `{ChangeEvents(): {...}}`: a fresh instance as the key adds the plugin to that viewset or
      action alone; the instance reads the value under its own key. While the configuration has a
      `ChangeEvents` instance as a key, a `ChangeEvents` that is not one of its keys, such as the
      one in `settings.viewsets_command_middleware`, publishes nothing.

    `True` and no value are the defaults. Any other value, or a dict with a key other than `record`
    and `topic`, raises `TypeError`.

    `execute()` resolves the topic, and for a `created` or `updated` op the pk field, before
    `call_next()`: a missing or malformed one raises before the action runs. `topic_for` and
    `changes_for` are the hooks for a subclass.
    """

    async def __call__(
        self,
        request: "Request | None",
        viewset: Any,  # noqa: ARG002
        context: Context,
        call_next: Callable[[], Awaitable[ViewSetResult]],
    ) -> ViewSetResult:
        if request is not None:
            topic = routed_base_path(request)
            if topic is not None:
                context.set(TOPIC_CONTEXT_KEY, topic)
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
                f"{type(self).__name__} is configured with {config!r}; it takes False, True or a dict of "
                f"'record' and 'topic'"
            )
        unknown = set(config) - _OPTIONS
        if unknown:
            raise TypeError(
                f"{type(self).__name__} is configured with unknown option(s) {', '.join(sorted(map(repr, unknown)))}; "
                f"it takes 'record' and 'topic'"
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
        topic = validate_topic(self.topic_for(execution))
        if op != "deleted":
            self.pk_field_for(execution)
        result = await call_next()
        await publish(topic, RecordsChanged(op=op, changes=self.changes_for(execution, op, result)))
        return result

    def topic_for(self, execution: ActionExecution) -> str:
        """
        The configured `topic` (a callable is called with `execution`); else, for an execution a
        request called (`execution.parent` None), the routed base path in the context under
        `TOPIC_CONTEXT_KEY`; else `__viewset_metadata__["base_path"]`, for an execution dispatched
        outside any call or in the execution of another action, whose context carries the topic of
        that other action. Raises `TypeError` when there is none of the three.
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
        returned record, its pk read from the field `pk_field_for(execution)` names, with `record`
        when the options set `record`. A `ViewSetResult` contributes its `body`.
        """
        bulk = execution.action in _BULK_ACTIONS
        if op == "deleted":
            pk = _argument(execution, "pk")
            return [Change(pk=one) for one in (pk if bulk else [pk])]
        body = result.body if isinstance(result, ViewSetResult) else result
        field = self.pk_field_for(execution)
        with_record = bool((self.options_for(execution.context) or {}).get("record", False))
        return [
            Change(pk=_read_pk(execution, record, field), record=_dump(record) if with_record else None)
            for record in (body if bulk else [body])
        ]


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


def _dump(record: Any) -> Any:
    if isinstance(record, BaseModel):
        return record.model_dump(mode="json")
    return to_jsonable_python(record)
