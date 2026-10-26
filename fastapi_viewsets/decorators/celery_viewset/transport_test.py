"""
The types of a `celery_viewset` call's arguments and result across the FastAPI process and the
worker: each value an action receives, and each value its caller receives, is of the type it is
when the action runs in the FastAPI process.

The in-process harness runs both ends in one process: `send_task` runs the task a
`celery_viewset_server` registered for a twin of the viewset class, with its kwargs passed through
JSON as Kombu passes them, and the result travels through a result list the FastAPI side's result
reader pops.
"""

import asyncio
import contextlib
import enum
import io
import json
import uuid

from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from unittest.mock import MagicMock

import pytest

from fastapi import APIRouter, BackgroundTasks, FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.testclient import TestClient
from muxws.frames import ABSENT
from pydantic import BaseModel
from starlette.datastructures import Headers, UploadFile as StarletteUploadFile
from starlette.responses import FileResponse, Response, StreamingResponse
from starlette.websockets import WebSocket

from fastapi_viewsets.context import SerializableObject
from fastapi_viewsets.decorators import celery_viewset_client, celery_viewset_server, route_viewset
from fastapi_viewsets.decorators.celery_viewset import get_result_queue_key, start_result_reader, stop_result_reader
from fastapi_viewsets.decorators.celery_viewset.transport import (
    ActionCodec,
    adapter_for,
    check_transportable,
    decode_value,
    encode_value,
    EncodedResult,
    response_from_jsonable,
    response_to_jsonable,
    upload_from_jsonable,
    upload_to_jsonable,
)
from fastapi_viewsets.middleware import ViewSetResult
from fastapi_viewsets.mux_ws.registry import reset_registry
from fastapi_viewsets.mux_ws.server import process_command
from fastapi_viewsets.mux_ws.server_test import FakeStream


class Kind(str, enum.Enum):
    LOGO = "logo"
    PDF = "pdf"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


class Point(BaseModel):
    x: int
    at: date


class Tagged(SerializableObject):
    """A type the application encodes itself."""

    def __serialize__(self):
        return {"v": self.value}

    @classmethod
    def __deserialize__(cls, data):
        return cls(data["v"])


@pytest.mark.skipif(not hasattr(enum, "StrEnum"), reason="enum.StrEnum is Python 3.11+")
def test_a_str_enum_member_crosses_as_its_member():
    file_format = enum.StrEnum("Format", ["PNG", "PDF"])
    result = _round_trip(file_format, file_format.PDF)
    assert result is file_format.PDF
    assert result.value == "pdf"


def test_a_serializable_object_argument_is_rebuilt_in_the_worker():
    async def store(self, token: Tagged, extra: Any) -> None: ...  # noqa: ARG001

    codec = ActionCodec(None, store)
    token, token_typed = codec.encode_argument("token", Tagged(5))
    extra, extra_typed = codec.encode_argument("extra", {"t": Tagged(6)})
    assert (token_typed, extra_typed) == (False, False)
    assert codec.decode_argument("token", json.loads(json.dumps(token)), token_typed).value == 5
    assert codec.decode_argument("extra", json.loads(json.dumps(extra)), extra_typed)["t"].value == 6


def test_positional_and_variadic_arguments_bind_to_their_parameters():
    async def many(self, first: date, *rest: int, **extra: str) -> None: ...  # noqa: ARG001

    codec = ActionCodec(None, many)
    assert codec.bind((date(2026, 1, 1), 2, 3), {"a": "b"}) == ((2, 3), {"first": date(2026, 1, 1), "a": "b"})
    with pytest.raises(TypeError):
        ActionCodec(None, _anything).bind((1,), {"value": 2})


def test_an_action_whose_hints_do_not_resolve_crosses_untyped():
    async def broken(self, day: "NoSuchType") -> None: ...  # noqa: ARG001, F821

    codec = ActionCodec(None, broken)
    assert codec.params["day"].kind == "untyped"
    assert codec.encode_argument("day", date(2026, 1, 1)) == ("2026-01-01", False)


def _round_trip(hint, value):
    """`value` encoded by `hint`, passed through JSON as Kombu passes it, and decoded."""
    adapter = adapter_for(hint)
    data, typed = encode_value(adapter, value)
    return decode_value(adapter, json.loads(json.dumps(data)), typed)


# ---------------------------------------------------------------------------
# value round trips
# ---------------------------------------------------------------------------

AT = datetime(2026, 10, 26, 9, 30, 15, 123456, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("hint", "value"),
    [
        (date, date(2026, 10, 26)),
        (datetime, AT),
        (datetime, datetime(2026, 10, 26, 9, 30)),
        (time, time(9, 30, 15)),
        (timedelta, timedelta(days=1, seconds=7, microseconds=3)),
        (uuid.UUID, uuid.UUID("12345678-1234-5678-1234-567812345678")),
        (Decimal, Decimal("1.50")),
        (Kind, Kind.PDF),
        (Level, Level.HIGH),
        (Literal["a", "b"], "b"),
        (set[int], {1, 2, 3}),
        (frozenset[str], frozenset({"a", "b"})),
        (tuple[int, str], (1, "a")),
        (tuple[date, ...], (date(2026, 1, 1), date(2026, 1, 2))),
        (list[Kind], [Kind.LOGO, Kind.PDF]),
        (dict[str, Decimal], {"a": Decimal("0.10")}),
        (dict[Kind, list[date]], {Kind.LOGO: [date(2026, 1, 1)]}),
        (date | None, None),
        (date | None, date(2026, 1, 1)),
        (int | Kind, Kind.LOGO),
        (PurePosixPath, PurePosixPath("/a/b")),
        (bytes, b"\x00\xff\x89PNG"),
        (bytes | None, b"\x01"),
        (list[bytes], [b"\x00", b"\xff"]),
        (dict[str, Point], {"p": Point(x=1, at=date(2026, 1, 1))}),
    ],
)
def test_a_value_crosses_as_its_type(hint, value):
    result = _round_trip(hint, value)
    assert result == value
    assert type(result) is type(value)


def test_a_value_its_hint_does_not_describe_crosses_as_json():
    assert _round_trip(date, "not a date") == "not a date"
    assert _round_trip(int, {"a": 1}) == {"a": 1}


def test_an_unannotated_value_crosses_as_json():
    assert _round_trip(None, {"at": AT}) == {"at": "2026-10-26T09:30:15.123456Z"}
    assert _round_trip(Any, [Decimal("1.5")]) == ["1.5"]


def test_a_serializable_object_crosses_at_any_depth():
    assert _round_trip(Tagged, Tagged(3)).value == 3
    nested = _round_trip(dict[str, Any], {"a": [Tagged(4)]})
    assert isinstance(nested["a"][0], Tagged)
    assert nested["a"][0].value == 4


def _upload(content: bytes, filename="logo.png", content_type="image/png") -> StarletteUploadFile:
    headers = Headers(
        raw=[
            (b"content-disposition", f'form-data; name="file"; filename="{filename}"'.encode()),
            (b"content-type", content_type.encode()),
        ]
    )
    return StarletteUploadFile(file=io.BytesIO(content), size=len(content), filename=filename, headers=headers)


def test_an_upload_crosses_with_its_name_headers_size_and_content():
    content = bytes(range(256)) * 10
    original = _upload(content)
    original.file.seek(5)

    upload = _round_trip(UploadFile, original)

    assert isinstance(upload, StarletteUploadFile)
    assert (upload.filename, upload.content_type, upload.size) == ("logo.png", "image/png", len(content))
    assert upload.headers.raw == original.headers.raw
    assert asyncio.run(upload.read()) == content
    asyncio.run(upload.seek(0))
    assert asyncio.run(upload.read(4)) == content[:4]
    assert original.file.tell() == 5


def test_a_list_of_uploads_crosses():
    uploads = _round_trip(list[UploadFile], [_upload(b"a", "a.txt", "text/plain"), _upload(b"b", "b.txt")])
    assert [(u.filename, u.file.read()) for u in uploads] == [("a.txt", b"a"), ("b.txt", b"b")]


def test_a_large_upload_is_spooled_to_a_file():
    upload = upload_from_jsonable(upload_to_jsonable(_upload(b"x" * (2 * 1024 * 1024))))
    assert upload.file._rolled  # noqa: SLF001 - SpooledTemporaryFile has no public flag
    assert upload.size == 2 * 1024 * 1024


def test_a_request_parameter_crosses_as_its_scope():
    scope = {"type": "http", "method": "GET", "path": "/a", "headers": [(b"x-a", b"1")], "query_string": b"q=1"}
    request = _round_trip(Request, Request(scope))
    assert isinstance(request, Request)
    assert (request.method, request.url.path, request.headers["x-a"], request.query_params["q"]) == (
        "GET",
        "/a",
        "1",
        "1",
    )


@pytest.mark.parametrize(
    "response",
    [
        Response(
            b"\x89PNG\x00", media_type="image/png", headers={"Content-Disposition": 'attachment; filename="a.png"'}
        ),
        JSONResponse({"a": 1}, status_code=201),
        HTMLResponse("<p>ž</p>"),
        PlainTextResponse("text", status_code=418),
        RedirectResponse("/elsewhere", status_code=303),
        Response(status_code=204),
    ],
)
def test_a_response_crosses_with_its_class_status_headers_and_body(response):
    response.set_cookie("session", "abc")
    crossed = response_from_jsonable(json.loads(json.dumps(response_to_jsonable(response))))
    assert type(crossed) is type(response)
    assert crossed.status_code == response.status_code
    assert crossed.raw_headers == response.raw_headers
    assert crossed.body == response.body
    assert crossed.media_type == response.media_type
    assert crossed.background is None


def test_a_response_of_a_local_subclass_crosses_as_its_importable_base():
    class Local(JSONResponse):
        pass

    crossed = response_from_jsonable(response_to_jsonable(Local({"a": 1})))
    assert type(crossed) is JSONResponse
    assert crossed.body == b'{"a":1}'


def test_a_response_class_from_the_payload_must_be_a_response():
    data = response_to_jsonable(Response(b"x"))
    with pytest.raises(TypeError, match="not a Response class"):
        response_from_jsonable({**data, "class": "no_such_module.Response"})
    with pytest.raises(TypeError, match="not a Response class"):
        response_from_jsonable({**data, "class": "starlette.responses.NoSuchResponse"})
    with pytest.raises(ValueError, match="base64"):
        response_from_jsonable({**data, "body": 1})
    with pytest.raises(TypeError, match="not a Response class"):
        response_from_jsonable({**data, "class": "builtins.dict"})
    with pytest.raises(TypeError, match="not a Response class"):
        response_from_jsonable({**data, "class": "starlette.responses.FileResponse"})


async def _chunks():
    yield b"x"


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (StreamingResponse(_chunks()), "iterator"),
        (FileResponse(__file__), "file read from the file system"),
        (BackgroundTasks(), "after the response is sent"),
        (WebSocket({"type": "websocket"}, receive=None, send=None), "connection held open"),
        (Response(b"x", background=BackgroundTasks()), "background task"),
    ],
)
def test_what_exists_only_in_its_process_does_not_cross(value, reason):
    encode = ActionCodec(None, _anything).encode_result if isinstance(value, Response) else check_transportable
    with pytest.raises(TypeError, match=reason):
        encode(value)
    with pytest.raises(TypeError, match=reason):
        encode_value(adapter_for(Any), value)


async def _anything(self, value: Any) -> Any:  # noqa: ARG001
    return value


# ---------------------------------------------------------------------------
# results by the return annotation
# ---------------------------------------------------------------------------


def test_a_result_crosses_as_the_return_annotation_states():
    async def report(self) -> dict[str, list[date]]: ...  # noqa: ARG001

    codec = ActionCodec(None, report)
    data, encoding = codec.encode_result({"a": [date(2026, 1, 1)]})
    assert encoding == "typed"
    assert codec.decode_result(EncodedResult(json.loads(json.dumps(data)), encoding)) == {"a": [date(2026, 1, 1)]}


def test_a_viewset_result_body_crosses_as_its_type_argument():
    async def report(self) -> ViewSetResult[Point]: ...  # noqa: ARG001

    codec = ActionCodec(None, report)
    value = ViewSetResult(body=Point(x=1, at=date(2026, 1, 1)), headers={"X-A": "1"}, status_code=201)
    data, encoding = codec.encode_result(value)
    crossed = codec.decode_result(EncodedResult(json.loads(json.dumps(data)), encoding))
    assert isinstance(crossed, ViewSetResult)
    assert crossed.body == value.body
    assert (crossed.headers, crossed.status_code) == ({"X-A": "1"}, 201)


# ---------------------------------------------------------------------------
# in-process worker over HTTP and muxws
# ---------------------------------------------------------------------------


class _ResultLists:
    """The Redis lists the worker pushes results to: `rpush` for the worker, `lpop` for the reader."""

    def __init__(self):
        self.lists: dict[str, deque] = defaultdict(deque)

    def rpush(self, key, value):
        self.lists[key].append(value)

    def expire(self, *_args):
        pass

    def reader(self):
        lists = self.lists

        class Reader:
            async def lpop(self, key):
                return lists[key].popleft().encode() if lists[key] else None

        return Reader()


MAX_UPLOAD = 1000


def make_files_viewset():
    class FilesViewSet:
        __router = APIRouter()

        @__router.get("days/{kind}")
        async def day(self, kind: Kind, day: date, levels: Annotated[list[Level], Query()]) -> dict:
            return {
                "kind": kind.value,
                "day_type": type(day).__name__,
                "weekday": day.isoweekday(),
                "levels": [type(level).__name__ for level in levels],
            }

        @__router.get("logo")
        async def logo(self) -> Response:
            return Response(
                b"\x89PNG\r\n\x1a\n\x00",
                media_type="image/png",
                headers={"Content-Disposition": 'attachment; filename="logo.png"'},
            )

        @__router.get("stream")
        async def stream(self) -> StreamingResponse:
            return StreamingResponse(_chunks())

        @__router.post("upload")
        async def upload(self, file: UploadFile, title: Annotated[str, Form()]) -> dict:
            if file.size > MAX_UPLOAD:
                raise HTTPException(413, "too large")
            content = await file.read()
            return {
                "class": type(file).__name__,
                "title": title,
                "filename": file.filename,
                "content_type": file.content_type,
                "size": file.size,
                "content": content.hex(),
            }

        @__router.post("uploads")
        async def uploads(self, files: list[UploadFile]) -> list[str]:
            return [f"{file.filename}:{len(await file.read())}" for file in files]

        @__router.post("later")
        async def later(self, tasks: BackgroundTasks) -> None:
            tasks.add_task(print)

    return FilesViewSet


@pytest.fixture
def files_app():
    reset_registry()
    lists = _ResultLists()
    tasks: dict = {}
    worker_celery = MagicMock()

    def register(name, **_options):
        def decorator(func):
            tasks[name] = func
            return func

        return decorator

    worker_celery.task.side_effect = register
    celery_viewset_server(worker_celery, "files", redis_client=lists)(make_files_viewset())

    client_celery = MagicMock()

    def send_task(name, args, kwargs, **_options):
        # the task has pushed its outcome; what it raises is recorded by Celery in the worker
        with contextlib.suppress(Exception):
            tasks[name](*json.loads(json.dumps(args)), **json.loads(json.dumps(kwargs)))

    client_celery.send_task.side_effect = send_task
    viewset = celery_viewset_client(client_celery, "files", redis_client=lists)(make_files_viewset())

    @asynccontextmanager
    async def lifespan(_app):
        await start_result_reader(lists.reader(), get_result_queue_key("files"), poll_interval=0.005)
        yield
        await stop_result_reader(get_result_queue_key("files"))

    app = FastAPI(lifespan=lifespan)
    router = APIRouter()
    route_viewset(router, base_path="/files")(viewset)
    app.include_router(router)
    yield app, lists
    reset_registry()


def test_a_date_query_and_an_enum_path_parameter_arrive_typed(files_app):
    app, _lists = files_app
    with TestClient(app) as client:
        response = client.get("/files/days/pdf", params={"day": "2026-10-26", "levels": [1, 2]})
    assert response.status_code == 200, response.text
    assert response.json() == {"kind": "pdf", "day_type": "date", "weekday": 1, "levels": ["Level", "Level"]}


def test_a_binary_response_arrives_with_its_headers(files_app):
    app, _lists = files_app
    with TestClient(app) as client:
        response = client.get("/files/logo")
    assert response.status_code == 200
    assert response.content == b"\x89PNG\r\n\x1a\n\x00"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == 'attachment; filename="logo.png"'
    assert response.headers["content-length"] == "9"


def test_an_upload_arrives_as_an_upload_file(files_app):
    app, _lists = files_app
    with TestClient(app) as client:
        response = client.post(
            "/files/upload",
            data={"title": "Logo"},
            files={"file": ("logo.png", b"\x89PNG\x00\xff", "image/png")},
        )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "class": "UploadFile",
        "title": "Logo",
        "filename": "logo.png",
        "content_type": "image/png",
        "size": 6,
        "content": "89504e4700ff",
    }


def test_the_action_checks_the_size_of_an_upload(files_app):
    app, _lists = files_app
    with TestClient(app) as client:
        response = client.post(
            "/files/upload", data={"title": "Big"}, files={"file": ("big.bin", b"x" * (MAX_UPLOAD + 1), "a/b")}
        )
    assert response.status_code == 413


def test_several_uploads_arrive(files_app):
    app, _lists = files_app
    with TestClient(app) as client:
        response = client.post("/files/uploads", files=[("files", ("a.txt", b"aa")), ("files", ("b.txt", b"b"))])
    assert response.status_code == 200, response.text
    assert response.json() == ["a.txt:2", "b.txt:1"]


def test_a_streaming_response_fails_as_a_result_that_cannot_cross(files_app):
    app, _lists = files_app
    with TestClient(app) as client:
        response = client.get("/files/stream")
    assert response.status_code == 500, response.text
    assert response.json()["detail_code"] == "fastapi-viewsets.action_failed"
    assert response.json()["detail_params"]["reason"] == "result"


def test_background_tasks_are_not_dispatched(files_app):
    app, _lists = files_app
    with TestClient(app) as client:
        response = client.post("/files/later")
    assert response.status_code == 503
    assert response.json()["detail_code"] == "fastapi-viewsets.action_dispatch_failed"
    assert response.json()["detail_params"]["reason"] == "serialization"


def test_a_result_the_caller_cannot_decode_fails_the_call(monkeypatch):
    from fastapi_viewsets.decorators.celery_viewset import result_reader
    from fastapi_viewsets.exceptions import ActionFailedError

    def register(_correlation_id, future, _queue_key=None):
        asyncio.get_event_loop().call_soon(lambda: future.set_result(EncodedResult("not a date", "typed")))

    monkeypatch.setattr(result_reader, "register_future", register)

    class Days:
        __router = APIRouter()

        @__router.get("today")
        async def today(self) -> date: ...

    viewset = celery_viewset_client(MagicMock(), "days", redis_client=MagicMock())(Days)
    with pytest.raises(ActionFailedError) as raised:
        asyncio.run(viewset().today())
    assert raised.value.params == {"task": "days.today", "reason": "result"}


@pytest.mark.asyncio
async def test_muxws_carries_the_same_types(files_app):
    app, _lists = files_app
    queue_key = get_result_queue_key("files")
    await start_result_reader(_lists.reader(), queue_key, poll_interval=0.005)
    try:
        stream = FakeStream(
            {":method": "GET", ":path": "/files/days/logo", ":query": {"day": "2026-10-27", "levels": [2]}}
        )
        await process_command(ABSENT, stream, app=app)
    finally:
        await stop_result_reader(queue_key)
    assert stream.status == 200, stream.payload
    assert stream.payload == {"kind": "logo", "day_type": "date", "weekday": 2, "levels": ["Level"]}
