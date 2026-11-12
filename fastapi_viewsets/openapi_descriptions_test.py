"""Every parameter and every field of a library model in a viewset's OpenAPI document has a description."""

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, Field

from .collection_viewset import CollectionViewSet
from .conf import settings
from .decorators import route_viewset
from .events.viewset import register_events
from .filters import make_filter_model
from .middleware.idempotency import Idempotency
from .mixins import BulkViewSetMixin, ListMixin, LookupMixin


class Track(BaseModel):
    id: int = Field(description="Catalogue number.")
    title: str = Field(description="Title of the track.")


TrackFilter = make_filter_model(Track, {"id": ["exact", "in"], "title": ["icontains"]})


def openapi() -> dict:
    app, router = FastAPI(), APIRouter()

    @route_viewset(router, base_path="/tracks", pk_field_name="id")
    class TrackViewSet(
        CollectionViewSet[int, Track], BulkViewSetMixin[int, Track], ListMixin[Track, TrackFilter], LookupMixin
    ):
        list_shapes = ("plain", "paginated", "cursor")

        def __init__(self):
            super().__init__(container={}, pk_field="id")

        async def perform_lookup(self, context):  # noqa: ARG002
            return []

    register_events(router)
    app.include_router(router)
    return app.openapi()


def test_every_parameter_is_described():
    undescribed = [
        f"{method.upper()} {path} {parameter['name']}"
        for path, operations in openapi()["paths"].items()
        for method, operation in operations.items()
        for parameter in operation.get("parameters", [])
        if not parameter.get("description")
    ]
    assert undescribed == []


def test_every_field_of_every_schema_is_described():
    undescribed = [
        f"{name}.{field}"
        for name, schema in openapi()["components"]["schemas"].items()
        for field, field_schema in schema.get("properties", {}).items()
        if not field_schema.get("description")
    ]
    assert undescribed == []


def test_every_operation_has_a_description_and_a_response_description():
    missing = [
        f"{method.upper()} {path}"
        for path, operations in openapi()["paths"].items()
        for method, operation in operations.items()
        if not operation.get("description")
        or any(
            response.get("description") == "Successful Response"
            for status, response in operation["responses"].items()
            if status.startswith("2")
        )
    ]
    assert missing == []


def test_every_page_model_is_described():
    schemas = openapi()["components"]["schemas"]
    pages = [name for name in schemas if name.startswith(("PaginatedList_", "CursorPage_", "ListOf_"))]
    assert pages
    assert [name for name in pages if not schemas[name].get("description")] == []


def test_the_bulk_updates_declare_the_404_and_describe_their_body():
    bulk = openapi()["paths"]["/tracks/bulk"]
    for method in ("put", "patch"):
        assert "404" in bulk[method]["responses"]
        assert "primary key" in bulk[method]["requestBody"]["description"]


def test_the_list_endpoint_alone_names_the_list_shape_and_cursor_codes():
    paths = openapi()["paths"]
    list_responses = paths["/tracks"]["get"]["responses"]
    assert "unsupported_list_shape" in list_responses["422"]["description"]
    assert "cursor_stale" in list_responses["default"]["description"]
    retrieve_responses = paths["/tracks/{pk}"]["get"]["responses"]
    assert "unsupported_list_shape" not in retrieve_responses["422"]["description"]
    assert "cursor_stale" not in retrieve_responses["default"]["description"]


def test_an_installed_idempotency_middleware_documents_its_header(monkeypatch):
    monkeypatch.setattr(settings, "viewsets_command_middleware", [Idempotency()])
    operation = openapi()["paths"]["/tracks/{pk}"]["get"]
    headers = [parameter for parameter in operation["parameters"] if parameter["in"] == "header"]
    assert [header["name"] for header in headers].count("Idempotency-Key") == 1


def test_without_idempotency_middleware_no_operation_has_its_header():
    names = [
        parameter["name"]
        for operations in openapi()["paths"].values()
        for operation in operations.values()
        for parameter in operation.get("parameters", [])
    ]
    assert "Idempotency-Key" not in names
