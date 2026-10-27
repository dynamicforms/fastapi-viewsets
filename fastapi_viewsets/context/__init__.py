import asyncio
import importlib
import inspect
import json

from abc import ABC, abstractmethod
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from ..conf import settings

if TYPE_CHECKING:
    from fastapi import Request

_TYPE_KEY = "__fpv_type__"
_VALUE_KEY = "__fpv_value__"


class ByAction:
    """
    Per-action variation of an @action_configuration value, e.g.
    `ByAction(list_items="read", update="write", default="read")`. Resolved at read time against the
    action the Context currently represents, so a clone for another action resolves afresh.
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
    A context value, or a `celery_viewset` argument or result, with custom (de)serialization across
    the Celery/Redis boundary. Subclasses implement __serialize__/__deserialize__; the payload is tagged
    with the subclass's dotted import path, so the class must be importable at module level.

    Awaiting it returns `value`, so Context field access is awaitable whether the value is eager or lazy.
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
    A SerializableObject whose value `resolve()` computes on first `await` (sync value or awaitable),
    once; concurrent awaiters share the result. Subclasses implement `resolve()`, `serialize_recipe()`
    and `deserialize_recipe()`: the recipe (e.g. a session id) travels instead of the value, plus the
    value when already resolved, which the deserialized copy reuses.

    Construction creates no Future: instances are rebuilt in a Celery worker before its event loop runs.
    """

    _UNSET = object()

    def __init__(self):
        self._future = None  # created in _ensure_future(), never at construction time
        self._preseeded = self._UNSET  # resolved value carried by the serialized payload

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
        """Compute the value: return it directly, or return an awaitable that yields it."""
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


class _Deferred:
    """
    A context value computed once, on its first `await`, by `compute()`. `celery_viewset_client` awaits
    every `_Deferred` before serializing arguments, so an exception of `compute()` propagates unchanged.
    """

    def __init__(self, compute: Any) -> None:
        self._compute = compute
        self._future: asyncio.Future | None = None

    def __await__(self):
        if self._future is None:
            self._future = asyncio.ensure_future(self._compute())
        return self._future.__await__()


class Context:
    """
    The per-request dict built by settings.viewsets_context_processors. `context.foo` and
    `context["foo"]` always return an awaitable, whether the value is eager or a LazyObject.

    __get_pydantic_core_schema__ (isinstance only) lets FastAPI register mixin routes that declare
    `context: Context`; route_viewset strips that parameter, so no request validates into a Context.
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

    @property
    def action_name(self) -> "str | None":
        """The action this context represents (None if built without one). It shadows a context value
        named `action_name`, which `context["action_name"]` still reads."""
        return self._action_name

    def raw(self) -> dict[str, Any]:
        """The underlying plain dict - for serialize_context()/deserialize_context() to walk."""
        return self._data

    def set(self, key: str, value: Any) -> None:
        """Add or replace a context value after construction, e.g. from a command middleware."""
        self._data[key] = value

    def configuration_for(self, identifier: Any) -> Any:
        """
        The @action_configuration value for `identifier` (a context processor or Middleware class),
        resolved against the current action; None if nothing is configured for it.
        """
        return _resolve_config_value(self._action_configuration.get(identifier), self._action_name)

    async def clone_for_command(self, action_name: "str | None" = None) -> "Context":
        """
        An independent copy for one command on a long-lived connection, so mutations never leak between
        commands. Goes through serialize_context()/deserialize_context() (not deepcopy) so SerializableObject
        values are rebuilt and resolved LazyObjects keep their value. A given `action_name` replaces the original's.
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


def serialize_value(value: Any) -> Any:
    """Tag a single SerializableObject for JSON transport as serialize_context() does; other values pass."""
    if isinstance(value, SerializableObject):
        return {_TYPE_KEY: _class_tag(type(value)), _VALUE_KEY: value.__serialize__()}
    return value


def to_jsonable(value: Any) -> Any:
    """
    Recursively convert a value to JSON-safe form: SerializableObjects are tagged, Pydantic models dumped,
    everything else goes through pydantic_core.to_jsonable_python() (FastAPI's response encoding).
    """
    if isinstance(value, SerializableObject):
        return serialize_value(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return to_jsonable_python(value)


def deserialize_value(value: Any) -> Any:
    """Inverse of serialize_value() - passes through anything that isn't a tagged payload."""
    if isinstance(value, dict) and _TYPE_KEY in value and _VALUE_KEY in value:
        return _resolve_class(value[_TYPE_KEY]).__deserialize__(value[_VALUE_KEY])
    return value


def from_jsonable(value: Any) -> Any:
    """Rebuild every tagged SerializableObject at any depth; all other values stay in JSON form."""
    if isinstance(value, dict):
        if _TYPE_KEY in value and _VALUE_KEY in value:
            return deserialize_value(value)
        return {k: from_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_jsonable(v) for v in value]
    return value


async def serialize_context(data: dict[str, Any]) -> dict[str, Any]:
    """
    A JSON-safe copy of a context dict: SerializableObjects are tagged, bare awaitables awaited, and the
    result round-tripped through settings.viewsets_context_json_encoder.
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
    """Inverse of serialize_context(). Synchronous: a Celery worker calls it before its event loop runs."""
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
    Run settings.viewsets_context_processors in order (later keys win) and return the Context.
    A processor declaring a parameter named `config` receives its resolved @action_configuration entry.
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


async def get_shared_context(
    request: "Request | None", viewset: Any, action_configuration: dict[Any, Any], action_name: str
) -> Context:
    """
    build_context() at most once per request (cached on request.state), shared by Middleware.depends()
    and the perform_* run. The first caller's `viewset` is what processors see. With `request` None
    nothing is cached.
    """
    if request is None:
        return await build_context(request, viewset, action_configuration, action_name)
    cached = getattr(request.state, "_context", None)
    if cached is not None:
        return cached
    context = await build_context(request, viewset, action_configuration, action_name)
    request.state._context = context
    return context
