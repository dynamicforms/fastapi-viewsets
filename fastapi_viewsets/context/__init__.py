import asyncio
import importlib
import inspect
import json

from abc import ABC, abstractmethod
from typing import Any, TYPE_CHECKING

from ..conf import settings

if TYPE_CHECKING:
    from fastapi import Request

_TYPE_KEY = "__type__"
_VALUE_KEY = "__value__"


class ByAction:
    """
    Per-action variation for an @action_configuration value (see fastapi_viewsets/action_configuration.py) -
    e.g. ByAction(list_items="read", update="write", default="read") lets one config entry mean
    different things depending on which action (list_items, update, ...) is currently running. Resolved at
    read-time (see Context.configuration_for), against whichever action name the Context currently
    represents - not baked in once when the configuration was merged - so a Context that later
    serves a different action (e.g. via clone_for_command with a new action_name) resolves fresh
    rather than reusing a stale value.
    """

    def __init__(self, default: Any = None, **by_action: Any):
        self._by_action = by_action
        self._default = default

    def resolve(self, action_name: "str | None") -> Any:
        return self._by_action.get(action_name, self._default)


def _resolve_config_value(value: Any, action_name: "str | None") -> Any:
    return value.resolve(action_name) if isinstance(value, ByAction) else value


class SerializableObject(ABC):
    """
    Base class for context values that need custom (de)serialization across the Celery/Redis
    boundary - e.g. because reconstructing them requires domain-specific logic, not just decoding
    a JSON literal (see settings.viewsets_context_json_encoder/decoder for the simpler case of
    normalising a plain value type like datetime).

    `value` holds the actual wrapped value. Subclasses implement the "magic" __serialize__/
    __deserialize__ methods; serialize_context()/deserialize_context() detect instances of this
    class within a context dict via isinstance and swap them for a tagged, JSON-safe payload
    carrying the dotted import path to the concrete subclass (module.QualName) - so deserialization
    can import that exact class and rebuild the right type without a separate registry.

    Awaitable even though it's eager/already-known - `Context` field access is always awaitable
    uniformly (see `Context` below), regardless of whether the underlying value is a plain eager
    SerializableObject or a lazily-resolved `LazyObject`; callers never need to branch on which.
    """

    value: Any

    def __init__(self, value: Any = None):
        self.value = value

    def __await__(self):
        async def _immediate():
            return self.value

        return _immediate().__await__()

    @abstractmethod
    def __serialize__(self) -> Any:
        """Return a JSON-safe payload sufficient for __deserialize__ to rebuild this object."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def __deserialize__(cls, data: Any) -> "SerializableObject":
        """Rebuild an instance from the payload produced by __serialize__."""
        raise NotImplementedError


class LazyObject(SerializableObject, ABC):
    """
    A SerializableObject whose actual value is computed lazily via `resolve()`, supporting two
    modes without the caller needing to know which one a given subclass uses:

    - Sync: `resolve()` returns the value directly - resolved (and memoized) on first `await`.
    - Async: `resolve()` returns an awaitable (typically `return self._fetch()` where `_fetch` is
      `async def`, or a Future/Task) - `await`-ed, once, on first access.

    Either way, the computation only runs once (memoized) and only if something actually awaits
    the object - a value nobody needs (e.g. because a `perform_*` override doesn't touch it) never
    pays for its computation. Concurrent `await obj` calls before resolution share the same
    underlying Future/Task rather than each independently triggering `resolve()`.

    Construction (`__init__`, and `__deserialize__` below) is deliberately loop-independent - no
    `asyncio.Future` is created until the first actual `await`/`_ensure_future()` call, which is
    only ever reachable from inside running async code. This matters because instances get
    reconstructed synchronously in `celery_viewset_server`'s `_reconstruct_kwargs`, before the
    worker's event loop starts.

    Crucially, __serialize__ carries whatever a subclass needs to resolve itself again later - the
    "recipe" (e.g. a session_id) - not just the resolved value, so resolution can happen
    wherever/whenever it's actually needed (the FastAPI process, a Celery worker, a per-command
    Context clone on a long-lived connection, or never at all). If already resolved at serialize
    time, the resolved value is included too, as a cheap shortcut - deserialize pre-seeds the new
    instance with it so it doesn't need to re-run resolution for something already known, while
    still building a fully independent instance (no shared mutable state).

    Subclasses implement __serialize_recipe__ inputs via `serialize_recipe()`/`deserialize_recipe()`
    (not __serialize__/__deserialize__ directly - those are provided, concrete, by this class) and
    the actual computation via `resolve()`.
    """

    _UNSET = object()

    def __init__(self):
        self._future = None  # created lazily in _ensure_future() - never at construction time
        self._preseeded = self._UNSET  # plain marker for a deserialize-time resolved shortcut

    def _ensure_future(self):
        if self._future is None:
            if self._preseeded is not self._UNSET:
                self._future = asyncio.get_running_loop().create_future()
                self._future.set_result(self._preseeded)
            else:
                result = self.resolve()
                if inspect.isawaitable(result):
                    self._future = asyncio.ensure_future(result)
                else:
                    self._future = asyncio.get_running_loop().create_future()
                    self._future.set_result(result)
        return self._future

    def __await__(self):
        return self._ensure_future().__await__()

    @property
    def value(self) -> Any:
        if self._preseeded is not self._UNSET:
            return self._preseeded
        if self._future is None or not self._future.done():
            raise RuntimeError(f"{type(self).__name__}.value accessed before it was awaited")
        return self._future.result()

    @property
    def is_resolved(self) -> bool:
        return self._preseeded is not self._UNSET or (self._future is not None and self._future.done())

    def __serialize__(self) -> Any:
        payload = {"recipe": self.serialize_recipe()}
        if self.is_resolved:
            payload["resolved_value"] = self.value
        return payload

    @classmethod
    def __deserialize__(cls, data: Any) -> "LazyObject":
        obj = cls.deserialize_recipe(data["recipe"])
        if "resolved_value" in data:
            obj._preseeded = data["resolved_value"]
        return obj

    @abstractmethod
    def resolve(self) -> Any:
        """
        Subclasses implement the actual computation (e.g. a session/DB lookup). Return the value
        directly for sync resolution, or an awaitable (typically just `return self._fetch()` where
        `_fetch` is `async def`) for async resolution - see the class docstring.
        """
        raise NotImplementedError

    @abstractmethod
    def serialize_recipe(self) -> Any:
        """Return a JSON-safe payload carrying whatever's needed to resolve this again later."""
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def deserialize_recipe(cls, data: Any) -> "LazyObject":
        """Reconstruct an (unresolved) instance from the payload produced by serialize_recipe()."""
        raise NotImplementedError


class Context:
    """
    Wraps the per-request context dict produced by running settings.viewsets_context_processors.
    Field access - both context.foo and context["foo"] - always returns something awaitable,
    uniformly, whether the underlying value is a plain eager value or a LazyObject: callers never
    need to know or branch on which. `await context.user` always works.

    Declares __get_pydantic_core_schema__ (isinstance-only validation) purely so FastAPI's eager
    route registration on the per-mixin `__router` instances in mixins.py doesn't choke on
    `context: Context` as a param type - those routes are never actually served (route_viewset
    strips the param out and builds context itself; celery mode never runs the FastAPI/pydantic
    validation path at all), so no real request ever needs to validate into a Context.
    """

    def __init__(
        self,
        data: dict[str, Any],
        action_configuration: dict[Any, Any] | None = None,
        action_name: "str | None" = None,
    ):
        self._data = data
        self._action_configuration = action_configuration or {}
        self._action_name = action_name

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type, handler):
        from pydantic_core import core_schema

        return core_schema.is_instance_schema(cls)

    def _get(self, key: str) -> Any:
        value = self._data[key]
        if isinstance(value, SerializableObject) or inspect.isawaitable(value):
            return value

        async def _immediate():
            return value

        return _immediate()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._get(name)

    def __getitem__(self, key: str) -> Any:
        return self._get(key)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def raw(self) -> dict[str, Any]:
        """The underlying plain dict - for serialize_context()/deserialize_context() to walk."""
        return self._data

    def set(self, key: str, value: Any) -> None:
        """
        Adds/replaces a context value after construction - e.g. a command middleware (which runs
        after context is already built) enriching context with something a later perform_* can
        read (see fastapi_viewsets.middleware.auth.Authorization's `context.authorization`).
        `value` is stored as-is; field access (`context.foo`/`context["foo"]`) wraps it awaitable
        exactly like any context-processor-contributed value, per the usual rules.
        """
        self._data[key] = value

    def configuration_for(self, identifier: Any) -> Any:
        """
        The @action_configuration value registered for `identifier` (typically a context processor
        callable or a Middleware (sub)class - see fastapi_viewsets/action_configuration.py),
        resolved against whichever action this Context currently represents. Returns None if
        nothing was configured for `identifier`. Command middleware normally reaches this via
        Middleware.config_from(context) rather than calling it directly.
        """
        return _resolve_config_value(self._action_configuration.get(identifier), self._action_name)

    async def clone_for_command(self, action_name: "str | None" = None) -> "Context":
        """
        An isolated copy of this context for one command on a long-lived connection (e.g. a WS
        message) - reuses serialize_context()/deserialize_context() rather than a generic
        copy.deepcopy(), so SerializableObject/LazyObject values are correctly reconstructed (and,
        via the recipe+resolved_value shortcut, an already-resolved LazyObject stays cheap to reuse
        across commands on the same connection) while every command still gets its own, fully
        independent Context instance - context and the ViewSet instance are the only mutable
        structures in the pipeline, and mutations from one command must never leak into another.

        The (unresolved) action_configuration dict is carried over unchanged - only `action_name`
        may differ, since a later command on the same connection can be a different action than the
        one that originally built this context; `configuration_for`/ByAction resolve fresh against
        whichever action_name the clone actually carries, rather than reusing a stale resolution.
        """
        return Context(
            deserialize_context(await serialize_context(self._data)),
            action_configuration=self._action_configuration,
            action_name=action_name if action_name is not None else self._action_name,
        )


def _class_tag(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _resolve_class(dotted_path: str) -> type[SerializableObject]:
    module_path, _, class_name = dotted_path.rpartition(".")
    try:
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
    except (ImportError, AttributeError) as e:
        raise TypeError(f"{dotted_path!r} does not resolve to an importable class") from e
    if not (isinstance(cls, type) and issubclass(cls, SerializableObject)):
        raise TypeError(f"{dotted_path!r} does not resolve to a SerializableObject subclass")
    return cls


async def serialize_context(data: dict[str, Any]) -> dict[str, Any]:
    """
    Returns a JSON-safe dict: SerializableObject values are swapped for a tagged payload (see
    SerializableObject), everything else is passed through as-is - except a bare awaitable (e.g. a
    processor put a raw Future/coroutine directly into context without wrapping it in
    SerializableObject), which is awaited first to get its plain value. Finally, everything is
    round-tripped through settings.viewsets_context_json_encoder (if set) to normalise plain
    non-JSON-native types (e.g. datetime).
    """
    tagged: dict[str, Any] = {}
    for key, val in data.items():
        if isinstance(val, SerializableObject):
            tagged[key] = {_TYPE_KEY: _class_tag(type(val)), _VALUE_KEY: val.__serialize__()}
        else:
            if inspect.isawaitable(val):
                val = await val
            tagged[key] = val
    return json.loads(json.dumps(tagged, cls=settings.viewsets_context_json_encoder))


def deserialize_context(data: dict[str, Any]) -> dict[str, Any]:
    """
    Inverse of serialize_context(). Stays synchronous and loop-independent - reconstructing a
    SerializableObject/LazyObject here never needs a running event loop (see LazyObject's
    docstring), which matters because this runs in celery_viewset_server's _reconstruct_kwargs,
    before the worker's event loop starts.
    """
    if settings.viewsets_context_json_decoder is not None:
        data = json.loads(json.dumps(data), cls=settings.viewsets_context_json_decoder)

    result: dict[str, Any] = {}
    for key, val in data.items():
        if isinstance(val, dict) and _TYPE_KEY in val and _VALUE_KEY in val:
            cls = _resolve_class(val[_TYPE_KEY])
            result[key] = cls.__deserialize__(val[_VALUE_KEY])
        else:
            result[key] = val
    return result


async def build_context(
    request: "Request | None",
    viewset: Any,
    action_configuration: dict[Any, Any] | None = None,
    action_name: "str | None" = None,
) -> Context:
    """
    Runs every processor in settings.viewsets_context_processors (in order, merging results -
    later processors win on key collisions) and returns the resulting Context.

    `action_configuration`/`action_name` (see fastapi_viewsets/action_configuration.py) are the
    per-call @action_configuration merge and the current action, respectively - both optional. A
    processor that wants its own configuration slice declares a parameter literally named `config`
    (detected by name, same convention as `context` in route_viewset.py); it's then called with
    that keyword set to its own resolved entry from `action_configuration` (or None if nothing was
    configured for it). A processor that doesn't declare `config` is called exactly as before - no
    existing processor needs to change.
    """
    action_configuration = action_configuration or {}
    merged: dict[str, Any] = {}
    for processor in settings.viewsets_context_processors:
        if "config" in inspect.signature(processor).parameters:
            config = _resolve_config_value(action_configuration.get(processor), action_name)
            merged.update(await processor(request, viewset, config=config))
        else:
            merged.update(await processor(request, viewset))
    return Context(merged, action_configuration=action_configuration, action_name=action_name)
