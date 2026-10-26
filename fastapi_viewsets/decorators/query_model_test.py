"""
A list or lookup filter model read from the query string: every field is a query parameter, a
sequence-typed field read from a repeated key, on REST, on muxws and across a Celery worker.
"""

import asyncio
import json

from typing import Annotated
from unittest.mock import MagicMock

import pytest

from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from muxws.frames import ABSENT
from pydantic import BaseModel, Field, model_validator

from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.context import Context
from fastapi_viewsets.decorators import celery_viewset_client, celery_viewset_server, route_viewset
from fastapi_viewsets.decorators.celery_viewset import result_reader
from fastapi_viewsets.error_responses import install_error_handlers
from fastapi_viewsets.mixins import ListMixin, LookupFilter, LookupItem, LookupMixin
from fastapi_viewsets.mux_ws.registry import reset_registry
from fastapi_viewsets.mux_ws.server import process_command
from fastapi_viewsets.mux_ws.server_test import FakeStream

from .query_model import query_model_dependency


class Track(BaseModel):
    id: int
    title: str
    tags: list[str] = []


TRACKS = {
    1: Track(id=1, title="So What", tags=["modal"]),
    2: Track(id=2, title="Blue in Green", tags=["ballad", "modal"]),
    3: Track(id=3, title="Giant Steps", tags=["bebop"]),
    4: Track(id=4, title="Naima", tags=["ballad"]),
}


class TrackLookupFilter(LookupFilter):
    ids: list[int] | None = Field(None, description="Primary keys of the tracks to return")


class TrackListFilter(BaseModel):
    title: str | None = Field(None, description="Exact title")
    tags: list[str] | None = Field(None, description="Tracks carrying any of these tags")


def make_viewset(decorate=None) -> type:
    class TrackViewSet(
        CollectionViewSet[int, Track], ListMixin[Track, TrackListFilter], LookupMixin[TrackLookupFilter]
    ):
        def __init__(self):
            super().__init__(container=TRACKS, pk_field="id")

        async def perform_lookup(self, _context: Context) -> list[LookupItem]:
            return [LookupItem(pk=track.id, title=track.title) for track in TRACKS.values()]

        async def filter_lookup(self, fltr: TrackLookupFilter, items: list[LookupItem]) -> list[LookupItem]:
            return [item for item in items if fltr.ids is None or item.pk in fltr.ids]

        async def filter_list(self, fltr: TrackListFilter, records: list[Track]) -> list[Track]:
            if fltr.title is not None:
                records = [track for track in records if track.title == fltr.title]
            if fltr.tags is not None:
                records = [track for track in records if set(fltr.tags) & set(track.tags)]
            return records

    if decorate is not None:
        TrackViewSet = decorate(TrackViewSet)  # noqa: N806 - the decorated class
    return TrackViewSet


def app_of(cls: type) -> FastAPI:
    router = APIRouter()
    route_viewset(router, base_path="/tracks", pk_field_name="id")(cls)
    app = FastAPI()
    app.include_router(router)
    install_error_handlers(app)
    return app


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_of(make_viewset()))


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("?ids=1&ids=3", [1, 3]),
        ("?ids=2", [2]),
        ("", [1, 2, 3, 4]),
        ("?q=n&ids=2&ids=4", [2, 4]),
    ],
)
def test_a_lookup_reads_a_sequence_field_from_repeated_keys(client, query, expected):
    response = client.get(f"/tracks/lookup{query}")
    assert response.status_code == 200
    assert [item["pk"] for item in response.json()] == expected


def test_a_list_reads_a_sequence_field_from_repeated_keys(client):
    response = client.get("/tracks?tags=bebop&tags=ballad")
    assert response.status_code == 200
    assert [track["id"] for track in response.json()] == [2, 3, 4]


def test_a_value_of_a_sequence_field_that_does_not_validate_is_a_422_under_the_parameter(client):
    response = client.get("/tracks/lookup?ids=1&ids=x")
    assert response.status_code == 422
    body = response.json()
    assert body["detail_code"] == "fastapi-viewsets.validation_failed"
    assert list(body["field_errors"]) == ["ids.1"]
    assert body["field_errors"]["ids.1"][0]["code"] == "pydantic.int_parsing"


def test_the_filter_is_not_read_from_a_body(client):
    operation = client.get("/tracks/schema").json()["paths"]["/tracks/lookup"]["get"]
    assert "requestBody" not in operation


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


def _parameters(document: dict, path: str) -> dict[str, dict]:
    return {p["name"]: p for p in document["paths"][path]["get"]["parameters"] if p["in"] == "query"}


@pytest.mark.parametrize("document", ["openapi", "schema"])
def test_the_schema_lists_every_filter_field_with_its_type_and_description(client, document):
    doc = client.get("/openapi.json" if document == "openapi" else "/tracks/schema").json()

    lookup = _parameters(doc, "/tracks/lookup")
    assert set(lookup) == {"q", "ids"}
    assert lookup["ids"]["description"] == "Primary keys of the tracks to return"
    assert {"type": "array", "items": {"type": "integer"}} in lookup["ids"]["schema"]["anyOf"]
    assert lookup["ids"]["required"] is False

    listing = _parameters(doc, "/tracks")
    assert {"title", "tags", "sort"} <= set(listing)
    assert listing["tags"]["description"] == "Tracks carrying any of these tags"
    assert {"type": "array", "items": {"type": "string"}} in listing["tags"]["schema"]["anyOf"]


# ---------------------------------------------------------------------------
# muxws
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected"),
    [({"ids": [1, 3]}, [1, 3]), ({"ids": 2}, [2]), (None, [1, 2, 3, 4]), ("ids=2&ids=4", [2, 4])],
)
async def test_a_lookup_over_muxws_reads_a_sequence_field(query, expected):
    headers = {":method": "GET", ":path": "/tracks/lookup"}
    if query is not None:
        headers[":query"] = query
    stream = FakeStream(headers)
    await process_command(ABSENT, stream, app=app_of(make_viewset()))
    assert stream.status == 200
    assert [item["pk"] for item in stream.payload] == expected


@pytest.mark.asyncio
async def test_a_sequence_value_that_does_not_validate_over_muxws_is_a_422():
    stream = FakeStream({":method": "GET", ":path": "/tracks/lookup", ":query": {"ids": [1, "x"]}})
    await process_command(ABSENT, stream, app=app_of(make_viewset()))
    assert stream.status == 422
    assert stream.payload["detail_code"] == "fastapi-viewsets.validation_failed"
    assert list(stream.payload["field_errors"]) == ["ids.1"]


# ---------------------------------------------------------------------------
# celery_viewset
# ---------------------------------------------------------------------------


def worker_tasks() -> dict:
    celery_app = MagicMock()
    registered: dict = {}

    def mock_task(name, **_kwargs):
        def decorator(func):
            registered[name] = func
            return func

        return decorator

    celery_app.task.side_effect = mock_task
    make_viewset(celery_viewset_server(celery_app=celery_app, task_prefix="tracks", redis_client=MagicMock()))
    return registered


def test_a_sequence_field_crosses_to_the_worker_as_json(monkeypatch):
    """The filter the request states reaches the task as JSON, and the worker filters by it."""
    celery_app = MagicMock()
    worker = worker_tasks()
    futures: list[asyncio.Future] = []
    sent: dict = {}

    def run_in_worker(name, *, args, kwargs, **_options):
        sent.update(json.loads(json.dumps(kwargs)))
        # without a correlation id the task answers directly instead of pushing to the result queue
        task_kwargs = {k: v for k, v in sent.items() if k not in {"_correlation_id", "_result_queue_key"}}
        result = worker[name](*args, **task_kwargs)
        future = futures.pop()
        future.get_loop().call_soon_threadsafe(future.set_result, [item.model_dump() for item in result])

    celery_app.send_task.side_effect = run_in_worker
    monkeypatch.setattr(result_reader, "register_future", lambda _id, future, _queue=None: futures.append(future))
    cls = make_viewset(celery_viewset_client(celery_app=celery_app, task_prefix="tracks", redis_client=MagicMock()))

    response = TestClient(app_of(cls)).get("/tracks/lookup?ids=2&ids=4")
    assert response.status_code == 200
    assert sent["fltr"] == {"q": None, "ids": [2, 4]}
    assert [item["pk"] for item in response.json()] == [2, 4]


# ---------------------------------------------------------------------------
# query_model_dependency
# ---------------------------------------------------------------------------


class Constrained(BaseModel):
    page: int = Field(1, ge=1, description="Page number")
    code: str | None = Field(None, max_length=3)
    kinds: list[str] = Field(default_factory=lambda: ["all"])
    kind_of: Annotated[str | None, Field(alias="kind")] = None
    required: int

    @model_validator(mode="after")
    def _reject_13(self):
        if self.required == 13:
            raise ValueError("13 is reserved")
        return self


@pytest.fixture
def constrained() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/items")
    def items(fltr: Annotated[Constrained, Depends(query_model_dependency(Constrained))]) -> dict:
        return {"fltr": fltr.model_dump(), "set": sorted(fltr.model_fields_set)}

    return TestClient(app)


def test_a_field_the_request_does_not_send_takes_the_models_default(constrained):
    body = constrained.get("/items?required=1").json()
    assert body == {
        "fltr": {"page": 1, "code": None, "kinds": ["all"], "kind_of": None, "required": 1},
        "set": ["required"],
    }


def test_a_field_is_read_from_its_alias(constrained):
    body = constrained.get("/items?required=1&kind=a&kinds=x&kinds=y").json()
    assert body["fltr"]["kind_of"] == "a"
    assert body["fltr"]["kinds"] == ["x", "y"]


@pytest.mark.parametrize(
    ("query", "field", "code"),
    [
        ("?required=1&page=0", "page", "pydantic.greater_than_equal"),
        ("?required=1&code=abcd", "code", "pydantic.string_too_long"),
        ("", "required", "pydantic.missing"),
        ("?required=13", "", "pydantic.value_error"),
    ],
)
def test_a_value_that_does_not_validate_is_a_422(constrained, query, field, code):
    response = constrained.get(f"/items{query}")
    assert response.status_code == 422
    errors = response.json()["field_errors"]
    assert list(errors) == [field]
    assert errors[field][0]["code"] == code


def test_the_parameters_carry_the_fields_constraints_defaults_and_aliases(constrained):
    parameters = {p["name"]: p for p in constrained.get("/openapi.json").json()["paths"]["/items"]["get"]["parameters"]}
    assert list(parameters) == ["page", "code", "kinds", "kind", "required"]
    assert parameters["page"]["schema"]["minimum"] == 1
    assert parameters["page"]["schema"]["default"] == 1
    assert parameters["page"]["description"] == "Page number"
    assert parameters["code"]["schema"]["anyOf"][0]["maxLength"] == 3
    assert parameters["required"]["required"] is True
    assert parameters["kinds"]["required"] is False


def test_the_dependency_of_a_model_is_built_once():
    assert query_model_dependency(Constrained) is query_model_dependency(Constrained)
