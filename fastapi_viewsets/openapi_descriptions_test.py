"""Every parameter and every field of a library model in a viewset's OpenAPI document has a description."""

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, Field

from .collection_viewset import CollectionViewSet
from .decorators import route_viewset
from .events.viewset import register_events
from .mixins import BulkViewSetMixin, LookupMixin


class Track(BaseModel):
    id: int = Field(description="Catalogue number.")
    title: str = Field(description="Title of the track.")


def openapi() -> dict:
    app, router = FastAPI(), APIRouter()

    @route_viewset(router, base_path="/tracks", pk_field_name="id")
    class TrackViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track], LookupMixin):
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
