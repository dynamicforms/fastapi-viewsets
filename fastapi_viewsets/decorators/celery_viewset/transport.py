"""
The encoding of a `celery_viewset` call's arguments and result between the FastAPI process and the
worker. Both ends build an `ActionCodec` for each action from the action's type hints (TypeVars
resolved against the viewset class), so the worker receives each argument, and the caller the
result, as the type the hint names.

An argument crosses one of three ways, decided by its parameter's hint:

- model: a hint that is a pydantic model, an optional model, or a list or tuple of models, and every
  body of a partial update. The model is dumped by field name and validated as its model in the
  worker; a create body without its primary key and a partial update body are validated as the
  FastAPI process validated them.
- typed: any other annotation except `Any`. The value is dumped in pydantic's JSON mode by a
  `TypeAdapter` of the annotation and validated from JSON with the same adapter: a `date` arrives as
  `date`, an enum member as that member, a `set` as a `set`, a `UUID`, `Decimal` or `datetime` as
  itself. `bytes`, `UploadFile` and `Request` are encoded by this module (see `transport_hint`), and
  an `Any` inside the annotation is encoded untyped.
- untyped: no annotation or `Any`, a None value, and a `SerializableObject` value. The value crosses
  as `fastapi_viewsets.context.to_jsonable` encodes it, and a `SerializableObject` in it is rebuilt
  by `__deserialize__` at any depth of lists and dicts.

A typed value raises `TypeError` naming the value, its declared type and its own type when pydantic
cannot build an adapter for the annotation, and when the adapter does not encode the value (its dump
reports an unexpected value: a `datetime` declared `date`, a dict declared as a model).

The result crosses by its value: a `Response` (see `response_to_jsonable`), a `ViewSetResult` (its
body by the `X` of a `ViewSetResult[X]` annotation), and any other value as an argument does, by
the return annotation with `ViewSetResult[X]` read as `X`.

A value that exists only in the process holding it raises `TypeError` naming the reason (see
`UNTRANSPORTABLE`).
"""

import base64
import functools
import importlib
import inspect
import json
import operator
import tempfile
import types

from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin, get_type_hints, Literal, Union

from pydantic import BaseModel, TypeAdapter
from pydantic_core import core_schema
from starlette.background import BackgroundTasks
from starlette.datastructures import Headers, UploadFile
from starlette.requests import HTTPConnection, Request
from starlette.responses import FileResponse, Response, StreamingResponse
from starlette.websockets import WebSocket

from ...context import from_jsonable, SerializableObject, to_jsonable
from ..primary_key_model_helper import partial_body_annotation, PARTIAL_UPDATE_ACTIONS
from ..route_viewset import build_type_map, resolve_typevars
from .payload import request_from_jsonable, request_to_jsonable

UPLOAD_SPOOL_SIZE = 1024 * 1024
"""Bytes of an `UploadFile` the worker keeps in memory; a larger file is spooled to a temporary file,
as Starlette spools a multipart upload."""

UNTRANSPORTABLE: dict[type, str] = {
    WebSocket: "a WebSocket is a connection held open by the FastAPI process",
    BackgroundTasks: "background tasks run in the FastAPI process after the response is sent",
    StreamingResponse: "its body is produced by an iterator in the process that returned it",
    FileResponse: "its body is a file read from the file system of the process that returned it",
}
"""The types that cannot cross between the processes, with the reason. A subclass is refused as its
base. A `Response` with a `background` task is refused as well."""


def _refusal(cls: type) -> str | None:
    for refused, reason in UNTRANSPORTABLE.items():
        if issubclass(cls, refused):
            return f"{cls.__name__} cannot cross to or from a Celery worker: {reason}"
    return None


def check_transportable(value: Any) -> None:
    """Raises `TypeError` for a value of a type in `UNTRANSPORTABLE` and for a `Response` with a
    `background` task."""
    reason = _refusal(type(value))
    if reason is not None:
        raise TypeError(reason)
    if isinstance(value, Response) and value.background is not None:
        raise TypeError(
            f"{type(value).__name__} with a background task cannot cross to or from a Celery worker: "
            "background tasks run in the FastAPI process after the response is sent"
        )


# Codecs pydantic uses in place of a type's own schema


class _Codec:
    """Annotated metadata replacing a type's schema: `encode` in JSON mode, `decode` for anything
    that is not already an instance of `cls`."""

    def __init__(self, cls: type, encode, decode):
        self.cls = cls
        self.encode = encode
        self.decode = decode

    def _validate(self, value: Any) -> Any:
        return value if isinstance(value, self.cls) else self.decode(value)

    def __get_pydantic_core_schema__(self, _source, _handler):
        return core_schema.no_info_plain_validator_function(
            self._validate,
            serialization=core_schema.plain_serializer_function_ser_schema(self.encode, when_used="json"),
        )


def _b64decode(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if not isinstance(value, str):
        raise ValueError("bytes cross as a base64 string")
    return base64.b64decode(value, validate=True)


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _raw_headers_to_jsonable(raw: list[tuple[bytes, bytes]]) -> list[list[str]]:
    return [[name.decode("latin-1"), value.decode("latin-1")] for name, value in raw]


def _raw_headers_from_jsonable(data: list[list[str]]) -> list[tuple[bytes, bytes]]:
    return [(name.encode("latin-1"), value.encode("latin-1")) for name, value in data]


def upload_to_jsonable(upload: UploadFile) -> dict[str, Any]:
    """`upload` with its whole content, read from its start; the file position is left unchanged.
    Reads synchronously from `upload.file`."""
    position = upload.file.tell()
    try:
        upload.file.seek(0)
        content = upload.file.read()
    finally:
        upload.file.seek(position)
    return {
        "filename": upload.filename,
        "size": upload.size if upload.size is not None else len(content),
        "headers": _raw_headers_to_jsonable(upload.headers.raw),
        "content": _b64encode(content),
    }


def upload_from_jsonable(data: dict[str, Any]) -> UploadFile:
    """A Starlette `UploadFile` over the content `upload_to_jsonable` encoded, positioned at its
    start. Content above `UPLOAD_SPOOL_SIZE` bytes is held in a temporary file."""
    file = tempfile.SpooledTemporaryFile(max_size=UPLOAD_SPOOL_SIZE)  # noqa: SIM115 - the UploadFile owns it
    file.write(_b64decode(data["content"]))
    file.seek(0)
    return UploadFile(
        file=file,
        size=data["size"],
        filename=data["filename"],
        headers=Headers(raw=_raw_headers_from_jsonable(data["headers"])),
    )


class _Untyped(_Codec):
    """An `Any` position inside a typed hint: encoded by `to_jsonable`, decoded by `from_jsonable`, so
    a `SerializableObject` in it is rebuilt."""

    def __init__(self):
        super().__init__(object, lambda value: _encode_untyped(value), from_jsonable)

    def _validate(self, value: Any) -> Any:
        return from_jsonable(value)


_UNTYPED = _Untyped()
_BYTES = _Codec(bytes, _b64encode, _b64decode)
_UPLOAD = _Codec(UploadFile, upload_to_jsonable, upload_from_jsonable)
_REQUEST = _Codec(HTTPConnection, request_to_jsonable, request_from_jsonable)


def _is_class(hint: Any) -> bool:
    """A plain class, not a parameterised generic (which `inspect.isclass` reports as a class on
    Python 3.10)."""
    return get_origin(hint) is None and inspect.isclass(hint)


def transport_hint(hint: Any) -> Any:
    """`hint` with each type this module encodes itself replaced by that type annotated with its
    codec, inside unions, `Annotated` and parameterised collections. A pydantic model's fields keep
    their own schema."""
    if hint is Any:
        return Annotated[Any, _UNTYPED]
    if _is_class(hint):
        if hint is bytes:
            return Annotated[bytes, _BYTES]
        if issubclass(hint, UploadFile):
            return Annotated[hint, _UPLOAD]
        if hint in (Request, HTTPConnection):
            return Annotated[hint, _REQUEST]
        return hint
    origin = get_origin(hint)
    if origin is None or origin is Literal:
        return hint
    args = get_args(hint)
    replaced = tuple(transport_hint(arg) if arg is not Ellipsis else arg for arg in args)
    if replaced == args:
        return hint
    if origin in (Union, types.UnionType):
        return functools.reduce(operator.or_, replaced)
    try:
        return origin[replaced if len(replaced) > 1 else replaced[0]]
    except TypeError:
        return hint


_UNBUILT = object()
_adapters: dict[Any, Any] = {}


def adapter_for(hint: Any) -> TypeAdapter | None:
    """The `TypeAdapter` of `hint` as `transport_hint` rewrites it, built once per hint; None for a
    hint pydantic cannot build one for, and for `Any`."""
    if hint is Any or hint is inspect.Parameter.empty or hint is None:
        return None
    try:
        cached = _adapters.get(hint, _UNBUILT)
    except TypeError:  # an unhashable hint is built every time
        return _build_adapter(hint)
    if cached is _UNBUILT:
        cached = _adapters[hint] = _build_adapter(hint)
    return cached


def _build_adapter(hint: Any) -> TypeAdapter | None:
    try:
        return TypeAdapter(transport_hint(hint))
    except Exception:
        return None


def _contains_upload(hint: Any) -> bool:
    if _is_class(hint):
        return issubclass(hint, UploadFile)
    return any(_contains_upload(arg) for arg in get_args(hint))


# Responses


def _importable_class(cls: type) -> type:
    """`cls`, or the nearest `Response` class in its MRO that its dotted path imports."""
    for candidate in cls.__mro__:
        if "<locals>" not in candidate.__qualname__ and issubclass(candidate, Response):
            return candidate
    return Response


def response_to_jsonable(response: Response) -> dict[str, Any]:
    """A `Response` whose body is in memory: its class, status code, raw headers (`Set-Cookie`,
    `Content-Length` and `Content-Type` included), body and media type. A class in `UNTRANSPORTABLE`
    and a response with a `background` task raise `TypeError`."""
    check_transportable(response)
    cls = _importable_class(type(response))
    data = {
        "class": f"{cls.__module__}.{cls.__qualname__}",
        "status_code": response.status_code,
        "headers": _raw_headers_to_jsonable(response.raw_headers),
        "body": _b64encode(bytes(response.body)),
    }
    if "media_type" in vars(response):
        data["media_type"] = response.media_type
    return data


def _response_class(dotted_path: str) -> type[Response]:
    module_path, _, qualname = dotted_path.rpartition(".")
    # a nested class's qualname has dots of its own: import the longest prefix that is a module
    parts = dotted_path.split(".")
    for split in range(len(parts) - 1, 0, -1):
        try:
            target: Any = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        try:
            for name in parts[split:]:
                target = getattr(target, name)
        except AttributeError:
            break
        if inspect.isclass(target) and issubclass(target, Response) and _refusal(target) is None:
            return target
        break
    raise TypeError(f"{module_path}.{qualname} is not a Response class that can cross from a Celery worker")


def response_from_jsonable(data: dict[str, Any]) -> Response:
    """The `Response` `response_to_jsonable` encoded, of the same class, built without its
    `__init__`: the status code, raw headers and body are the encoded ones, unchanged."""
    cls = _response_class(data["class"])
    response = cls.__new__(cls)
    response.status_code = data["status_code"]
    response.background = None
    response.body = _b64decode(data["body"])
    response.raw_headers = _raw_headers_from_jsonable(data["headers"])
    if "media_type" in data:
        response.media_type = data["media_type"]
    return response


# Values


@dataclass(frozen=True)
class EncodedResult:
    """A result as the worker pushed it, with the way it was encoded; `ActionCodec.decode_result`
    decodes it."""

    data: Any
    encoding: str


def _encode_typed(adapter: TypeAdapter, value: Any) -> Any:
    return adapter.dump_python(value, mode="json", by_alias=False, warnings="error")


def _decode_typed(adapter: TypeAdapter, data: Any) -> Any:
    return adapter.validate_json(json.dumps(data), by_name=True)


def _encode_untyped(value: Any) -> Any:
    check_transportable(value)
    return to_jsonable(value)


def declares_type(hint: Any) -> bool:
    """Whether `hint` is an annotation that types a value: neither absent nor `Any`."""
    return hint is not None and hint is not Any and hint is not inspect.Parameter.empty


def _type_name(hint: Any) -> str:
    if get_origin(hint) is Annotated:
        return _type_name(get_args(hint)[0])
    return hint.__name__ if _is_class(hint) else repr(hint)


def encode_value(hint: Any, value: Any, what: str = "the value") -> tuple[Any, bool]:
    """`value` encoded by `hint`'s adapter, and True; or encoded untyped, and False, for a `hint`
    that types nothing (see `declares_type`), a None value and a `SerializableObject`. `what` names
    the value in the `TypeError` raised for a declared type pydantic cannot validate and for a value
    that does not encode as its declared type."""
    check_transportable(value)
    if not declares_type(hint) or value is None or isinstance(value, SerializableObject):
        return _encode_untyped(value), False
    adapter = adapter_for(hint)
    if adapter is None:
        raise TypeError(
            f"{what} is declared {_type_name(hint)}, which pydantic cannot validate; the value is a "
            f"{type(value).__name__}. Declare a type pydantic validates, or make the value a SerializableObject"
        )
    try:
        return _encode_typed(adapter, value), True
    except Exception as error:
        raise TypeError(
            f"{what} is declared {_type_name(hint)} and the value, a {type(value).__name__}, does not encode as it"
        ) from error


def decode_value(adapter: TypeAdapter | None, data: Any, typed: bool) -> Any:
    if typed and adapter is not None:
        return _decode_typed(adapter, data)
    return from_jsonable(data)


def _unwrap_optional(hint):
    if get_origin(hint) in (Union, types.UnionType):
        args = [arg for arg in get_args(hint) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return hint


def is_model_hint(hint: Any) -> bool:
    """A model, an optional model, or a list or tuple of models."""
    hint = _unwrap_optional(hint)
    if _is_class(hint) and issubclass(hint, BaseModel):
        return True
    if get_origin(hint) in (list, tuple):
        args = get_args(hint)
        return bool(args) and _is_class(args[0]) and issubclass(args[0], BaseModel)
    return False


@dataclass(frozen=True)
class ParamCodec:
    name: str
    hint: Any
    kind: str
    """`model`, `partial`, `typed` or `untyped` (see the module docstring)."""


def _resolved_hints(cls: type | None, endpoint, include_extras: bool = False) -> dict[str, Any]:
    try:
        hints = get_type_hints(endpoint, include_extras=include_extras)
    except Exception:
        return {}
    type_map = build_type_map(cls) if cls is not None else {}
    return {name: resolve_typevars(type_map, hint) for name, hint in hints.items()}


class ActionCodec:
    """The encoding of one action's arguments and result, from `endpoint`'s type hints resolved
    against `cls`. `context` and `request` are not arguments: they cross as the call's data."""

    def __init__(self, cls: type | None, endpoint):
        self.task_name = endpoint.__name__
        self.signature = inspect.signature(endpoint)
        # the model paths read the bare annotation; a typed value is encoded by the annotation with
        # its `Annotated` metadata, which may carry its validator and serializer
        hints = _resolved_hints(cls, endpoint)
        annotated = _resolved_hints(cls, endpoint, include_extras=True)
        partial = endpoint.__name__ in PARTIAL_UPDATE_ACTIONS
        self.params: dict[str, ParamCodec] = {}
        for name, parameter in self.signature.parameters.items():
            if name in ("self", "context", "request") or parameter.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            hint = hints.get(name)
            if hint is None:
                self.params[name] = ParamCodec(name, None, "untyped")
            elif partial and partial_body_annotation(hint, None) is not None:
                self.params[name] = ParamCodec(name, hint, "partial")
            elif is_model_hint(hint):
                self.params[name] = ParamCodec(name, hint, "model")
            else:
                hint = annotated.get(name, hint)
                self.params[name] = ParamCodec(name, hint, "typed" if declares_type(hint) else "untyped")
        self.reads_uploads = any(_contains_upload(codec.hint) for codec in self.params.values() if codec.hint)
        self.result_hint = annotated.get("return")

    def bind(self, args: tuple, kwargs: dict) -> tuple[tuple, dict]:
        """`args` and `kwargs` of a call as keyword arguments by parameter name, beside the values
        of a `*args` parameter. Raises `TypeError` for arguments the signature does not accept."""
        bound = self.signature.bind_partial(None, *args, **kwargs)
        positional: tuple = ()
        named: dict = {}
        for name, value in list(bound.arguments.items())[1:]:
            kind = self.signature.parameters[name].kind
            if kind is inspect.Parameter.VAR_POSITIONAL:
                positional = value
            elif kind is inspect.Parameter.VAR_KEYWORD:
                named.update(value)
            else:
                named[name] = value
        return positional, named

    def encode_argument(self, name: str, value: Any) -> tuple[Any, bool]:
        """`value` of parameter `name` encoded typed, and True, or untyped, and False. A model
        argument (`model`, `partial`) is encoded by the caller and reported typed. Raises `TypeError`
        naming the action for a value that does not cross (see `encode_value`)."""
        codec = self.params.get(name)
        try:
            return encode_value(codec.hint if codec is not None else None, value, f"argument {name!r}")
        except TypeError as error:
            raise TypeError(f"{self.task_name}: {error}") from error

    def decode_argument(self, name: str, data: Any, typed: bool) -> Any:
        codec = self.params.get(name)
        return decode_value(adapter_for(codec.hint) if codec is not None else None, data, typed)

    def encode_result(self, value: Any) -> tuple[Any, str]:
        """The result as the worker pushes it, and its encoding: `response`, `viewset_result`,
        `typed` or `untyped`."""
        try:
            return _encode_result(value, self.result_hint)
        except TypeError as error:
            raise TypeError(f"{self.task_name}: {error}") from error

    def decode_result(self, result: EncodedResult) -> Any:
        return _decode_result(result.data, result.encoding, self.result_hint)


def _body_hint(hint: Any) -> Any:
    from ...middleware import unwrap_viewset_result_type

    return unwrap_viewset_result_type(hint)


def _encode_result(value: Any, hint: Any) -> tuple[Any, str]:
    from ...middleware import ViewSetResult

    if isinstance(value, Response):
        return response_to_jsonable(value), "response"
    if isinstance(value, ViewSetResult):
        body, body_encoding = _encode_result(value.body, _body_hint(hint))
        return {
            "body": body,
            "body_encoding": body_encoding,
            "headers": to_jsonable(value.headers),
            "cookies": to_jsonable(value.cookies),
            "status_code": value.status_code,
        }, "viewset_result"
    data, typed = encode_value(_body_hint(hint), value, "the result")
    return data, "typed" if typed else "untyped"


def _decode_result(data: Any, encoding: str, hint: Any) -> Any:
    from ...middleware import ViewSetResult

    if encoding == "response":
        return response_from_jsonable(data)
    if encoding == "viewset_result":
        return ViewSetResult(
            body=_decode_result(data["body"], data["body_encoding"], _body_hint(hint)),
            headers=data["headers"],
            cookies=data["cookies"],
            status_code=data["status_code"],
        )
    if encoding == "typed":
        return decode_value(adapter_for(_body_hint(hint)), data, True)
    return from_jsonable(data)
