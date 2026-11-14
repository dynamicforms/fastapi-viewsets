"""
`error_field`: the viewset method that names the field of every field error, on each path that builds
`field_errors`.
"""

import pytest

from django.core.exceptions import ValidationError as DjangoValidationError
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from .collection_viewset import CollectionViewSet
from .conf import settings
from .context import Context
from .decorators import route_viewset, ViewSetRouter
from .error_responses import install_error_handlers
from .middleware.idempotency import Idempotency
from .middleware.idempotency_test import FakeRedis, KEY
from .mixins import BulkViewSetMixin, ListMixin, ViewSetBase
from .mux_ws.server import process_command
from .mux_ws.server_test import FakeStream


class Track(BaseModel):
    id: int
    title: str = Field(min_length=3)
    year: int = 2000


calls: list[tuple[str | None, str, str]] = []


def renaming(_self, context: Context, field: str, code: str) -> str:
    """Records each call; `title` becomes `name`, a `django.unique` or `pydantic.int_parsing` error goes on the form."""
    calls.append((context.action_name, field, code))
    if field == "title":
        return "name"
    if code in ("django.unique", "pydantic.int_parsing"):
        return ""
    return field


def _taken(field: str = "year") -> DjangoValidationError:
    return DjangoValidationError(
        {
            "title": DjangoValidationError("Too short", code="min_length", params={"limit_value": 3}),
            field: DjangoValidationError("Taken", code="unique", params={"unique_check": (field,)}),
        }
    )


TOO_SHORT = {"code": "fastapi-viewsets.min_length", "params": {"minLength": 3}, "detail": "Too short"}
TAKEN = {"code": "fastapi-viewsets.unique", "params": {"fields": ["year"]}, "detail": "Taken"}


@pytest.fixture(autouse=True)
def _reset():
    calls.clear()
    yield
    settings.viewsets_command_middleware = []
    settings.viewsets_context_processors = []


def tracks_app(lifecycle: str = "singleton", error_field=renaming) -> FastAPI:
    router = APIRouter()

    class TrackViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        __router = APIRouter()

        def __init__(self):
            super().__init__({1: Track(id=1, title="One"), 2: Track(id=2, title="Two")}, pk_field="id")

        @__router.post("taken")
        async def taken(self) -> int:
            raise _taken()

        @__router.get("count")
        async def count(self, title: int) -> int:
            return title

        async def perform_destroy(self, context: Context, pk: int) -> dict[int, None]:
            if pk == 2:
                raise _taken()
            return await super().perform_destroy(context, pk)

        async def perform_bulk_create(self, context: Context, data: list[Track]) -> list[Track]:
            if any(record.title == "Taken" for record in data):
                raise _taken()
            return await super().perform_bulk_create(context, data)

        async def perform_bulk_update(
            self, context: Context, records: dict[int, Track], partial: bool = True
        ) -> list[Track]:
            if any(record.title == "Taken" for record in records.values()):
                raise _taken()
            return await super().perform_bulk_update(context, records, partial)

    TrackViewSet.error_field = error_field
    route_viewset(router, "/tracks", lifecycle=lifecycle, pk_field_name="id")(TrackViewSet)
    app = FastAPI()
    app.include_router(router)
    return app


def test_every_viewset_mixin_has_error_field_returning_the_field():
    for mixin in (BulkViewSetMixin, ListMixin, CollectionViewSet):
        assert issubclass(mixin, ViewSetBase)
    assert ViewSetBase().error_field(Context({}), "title", "pydantic.string_too_short") == "title"


def test_request_validation_of_the_body_names_the_fields_through_error_field():
    response = TestClient(tracks_app()).post("/tracks", json={"title": "ab", "year": "x"})
    assert response.status_code == 422
    assert response.json()["field_errors"] == {
        "name": [
            {
                "code": "fastapi-viewsets.min_length",
                "params": {"minLength": 3},
                "detail": "String should have at least 3 characters",
            }
        ],
        "": [
            {
                "code": "fastapi-viewsets.invalid_integer",
                "params": {},
                "detail": "Input should be a valid integer, unable to parse string as an integer",
            }
        ],
    }
    assert calls == [("create", "title", "pydantic.string_too_short"), ("create", "year", "pydantic.int_parsing")]


def test_request_validation_of_the_path_and_query_names_the_fields_through_error_field():
    client = TestClient(tracks_app())
    assert client.get("/tracks/abc").json()["field_errors"] == {
        "": [
            {
                "code": "fastapi-viewsets.invalid_integer",
                "params": {},
                "detail": "Input should be a valid integer, unable to parse string as an integer",
            }
        ]
    }
    assert list(client.get("/tracks/count", params={"title": "x"}).json()["field_errors"]) == ["name"]
    assert calls == [("retrieve", "pk", "pydantic.int_parsing"), ("count", "title", "pydantic.int_parsing")]


def test_a_body_level_error_is_passed_with_an_empty_field_and_can_be_moved():
    def to_title(_self, _context, field, _code):
        return field or "title"

    response = TestClient(tracks_app(error_field=to_title)).post("/tracks", json=[1])
    assert list(response.json()["field_errors"]) == ["title"]


def test_a_django_validation_error_of_an_action_names_the_fields_with_its_django_code():
    response = TestClient(tracks_app()).post("/tracks/taken")
    assert (response.status_code, response.json()["field_errors"]) == (422, {"name": [TOO_SHORT], "": [TAKEN]})
    assert calls == [("taken", "title", "django.min_length"), ("taken", "year", "django.unique")]


def test_a_per_request_viewset_names_the_fields_too():
    response = TestClient(tracks_app(lifecycle="per-request")).post("/tracks/taken")
    assert response.json()["field_errors"] == {"name": [TOO_SHORT], "": [TAKEN]}


def test_a_bulk_action_passes_the_field_after_the_rows_key_and_keeps_the_key():
    client = TestClient(tracks_app())
    created = client.post("/tracks/bulk", json=[{"id": 3, "title": "Fine"}, {"id": 4, "title": "ab", "year": "x"}, 5])
    assert list(created.json()["field_errors"]) == ["1.name", "1", "2"]
    updated = client.patch("/tracks/bulk", json={"1": {"title": "ab"}})
    assert list(updated.json()["field_errors"]) == ["1.name"]
    assert calls == [
        ("bulk_create", "title", "pydantic.string_too_short"),
        ("bulk_create", "year", "pydantic.int_parsing"),
        ("bulk_create", "", "pydantic.model_attributes_type"),
        ("bulk_partial_update", "title", "pydantic.string_too_short"),
    ]


def test_a_django_validation_error_of_a_bulk_perform_method_is_passed_with_its_field_and_no_row_key():
    client = TestClient(tracks_app())
    created = client.post("/tracks/bulk", json=[{"id": 3, "title": "Taken"}])
    assert (created.status_code, created.json()["field_errors"]) == (422, {"name": [TOO_SHORT], "": [TAKEN]})
    updated = client.patch("/tracks/bulk", json={"1": {"title": "Taken"}})
    assert updated.json()["field_errors"] == {"name": [TOO_SHORT], "": [TAKEN]}
    assert calls == [
        ("bulk_create", "title", "django.min_length"),
        ("bulk_create", "year", "django.unique"),
        ("bulk_partial_update", "title", "django.min_length"),
        ("bulk_partial_update", "year", "django.unique"),
    ]


def test_an_idempotent_repeat_of_a_bulk_action_answers_the_same_names():
    settings.viewsets_command_middleware = [Idempotency(redis_client=FakeRedis())]
    client = TestClient(tracks_app())
    first = client.post("/tracks/bulk", json=[{"id": 3, "title": "Taken"}], headers={"Idempotency-Key": KEY})
    repeat = client.post("/tracks/bulk", json=[{"id": 3, "title": "Taken"}], headers={"Idempotency-Key": KEY})
    assert first.json() == repeat.json()
    assert first.json()["field_errors"] == {"name": [TOO_SHORT], "": [TAKEN]}


async def _reject_without_authorization(request, _viewset):
    if "authorization" not in request.headers:
        raise HTTPException(401, "no auth")
    return {"user": "u"}


@pytest.mark.parametrize("error_field", [renaming, ViewSetBase.error_field])
def test_malformed_json_answers_422_without_running_the_context_processors(error_field):
    settings.viewsets_context_processors = [_reject_without_authorization]
    response = TestClient(tracks_app(error_field=error_field)).post(
        "/tracks", content=b"{bad", headers={"content-type": "application/json"}
    )
    assert (response.status_code, response.json()["detail_code"]) == (422, "fastapi-viewsets.validation_failed")
    assert response.json()["field_errors"] == {
        "1": [
            {
                "code": "pydantic.json_invalid",
                "params": {"error": "Expecting property name enclosed in double quotes"},
                "detail": "JSON decode error",
            }
        ]
    }
    assert calls == ([("create", "1", "pydantic.json_invalid")] if error_field is renaming else [])


def test_error_field_receives_the_values_of_the_requests_context():
    async def processor(_request, _viewset):
        return {"user": "u"}

    seen = []

    def by_user(_self, context: Context, field: str, _code: str) -> str:
        seen.append(context.raw()["user"])
        return field

    settings.viewsets_context_processors = [processor]
    TestClient(tracks_app(error_field=by_user)).post("/tracks/taken")
    assert seen == ["u", "u"]


def test_a_bulk_destroy_names_the_fields_of_each_keys_error():
    response = TestClient(tracks_app()).request("DELETE", "/tracks/bulk", json=[1, 2])
    assert response.status_code == 200
    assert response.json()["1"] is None
    assert response.json()["2"]["field_errors"] == {"name": [TOO_SHORT], "": [TAKEN]}
    assert calls == [("bulk_destroy", "title", "django.min_length"), ("bulk_destroy", "year", "django.unique")]


@pytest.mark.asyncio
async def test_muxws_names_the_fields_as_rest_does():
    app = tracks_app()
    stream = FakeStream({":method": "POST", ":path": "/tracks/taken"})
    await process_command(None, stream, app=app)
    assert (stream.status, stream.payload["field_errors"]) == (422, {"name": [TOO_SHORT], "": [TAKEN]})
    stream = FakeStream({":method": "POST", ":path": "/tracks"})
    await process_command({"title": "ab"}, stream, app=app)
    assert list(stream.payload["field_errors"]) == ["name"]


def test_an_idempotent_repeat_answers_the_same_names_from_the_source_codes():
    settings.viewsets_command_middleware = [Idempotency(redis_client=FakeRedis())]
    client = TestClient(tracks_app())
    first = client.post("/tracks/taken", headers={"Idempotency-Key": KEY})
    repeat = client.post("/tracks/taken", headers={"Idempotency-Key": KEY})
    assert first.json() == repeat.json()
    assert first.json()["field_errors"] == {"name": [TOO_SHORT], "": [TAKEN]}
    assert calls == [("taken", "title", "django.min_length"), ("taken", "year", "django.unique")] * 2


def test_a_route_outside_the_viewsets_keeps_the_names():
    app = tracks_app()
    install_error_handlers(app)

    @app.post("/plain")
    async def plain():
        raise _taken()

    response = TestClient(app).post("/plain")
    assert response.json()["field_errors"] == {"title": [TOO_SHORT], "year": [TAKEN]}
    assert calls == []


def test_a_sub_viewset_names_the_fields_of_its_own_operations():
    router = APIRouter()

    class MembersViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__({}, pk_field="id")

        error_field = renaming

    @route_viewset(router, "/accounts", pk_field_name="id")
    class AccountViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        __router = ViewSetRouter()
        __router.sub_viewset("members", MembersViewSet, pk_field_name="id")

        def __init__(self):
            super().__init__({}, pk_field="id")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert list(client.post("/accounts/members", json={"title": "ab"}).json()["field_errors"]) == ["name"]
    assert list(client.post("/accounts", json={"title": "ab"}).json()["field_errors"]) == ["title"]


def test_an_error_field_that_raises_answers_500_unhandled_error():
    def failing(_self, _context, _field, _code):
        raise RuntimeError("broken")

    response = TestClient(tracks_app(error_field=failing), raise_server_exceptions=False).post("/tracks/taken")
    assert (response.status_code, response.json()["detail_code"]) == (500, "fastapi-viewsets.unhandled_error")
