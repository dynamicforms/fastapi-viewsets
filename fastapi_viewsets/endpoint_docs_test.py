"""Per-viewset documentation for endpoints a mixin provided."""

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from .collection_viewset import CollectionViewSet
from .decorators import route_viewset
from .endpoint_docs import docs_for, endpoint_docs
from .mixins import BulkViewSetMixin


class Track(BaseModel):
    id: int = Field(json_schema_extra={"autoinc_int": True})
    title: str


DATABASE = {1: Track(id=1, title="Kind of Blue")}

DOCS = {
    "list_items": {"summary": "Browse the library", "description": "Oldest first."},
    "create": {"summary": "Add a track", "deprecated": True},
}


def documented_client(docs=None) -> TestClient:
    app, router = FastAPI(), APIRouter()

    @route_viewset(router, base_path="/t", pk_field_name="id")
    @endpoint_docs(docs if docs is not None else DOCS)
    class TrackViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=DATABASE, pk_field="id")

    app.include_router(router)
    return TestClient(app)


def paths(client: TestClient) -> dict:
    return client.get("/t/schema").json()["paths"]


def test_a_documented_action_carries_its_own_wording():
    """
    The whole point: the mixin's docstring is the same sentence on every viewset in an application,
    so it cannot say anything about this one.
    """
    operation = paths(documented_client())["/t"]["get"]
    assert operation["summary"] == "Browse the library"
    assert operation["description"] == "Oldest first."


def test_fields_other_than_text_come_through_too():
    assert paths(documented_client())["/t"]["post"]["deprecated"] is True


def test_an_undocumented_action_keeps_the_mixin_wording():
    """Documenting one endpoint must not blank the others."""
    assert paths(documented_client())["/t/{pk}"]["get"]["summary"] == "Retrieve"


def test_the_user_facing_router_is_documented_as_well_as_the_schema_one():
    """
    Two routers are built from one declaration - the app's and the one /schema reports on - and a
    fix applied to only one of them is the bug this test exists for.
    """
    app, router = FastAPI(), APIRouter()

    @route_viewset(router, base_path="/t", pk_field_name="id")
    @endpoint_docs(DOCS)
    class TrackViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=DATABASE, pk_field="id")

    app.include_router(router)
    served = FastAPI()
    served.include_router(router)
    operation = TestClient(served).get("/openapi.json").json()["paths"]["/t"]["get"]
    assert operation["summary"] == "Browse the library"


def test_an_unknown_action_name_is_refused():
    """A typo would otherwise leave the endpoint on the mixin's wording, looking like no entry."""
    with pytest.raises(ValueError, match="endpoint\\(s\\) it does not have"):
        documented_client({"list_itemz": {"summary": "typo"}})


def test_putting_the_decorator_above_route_viewset_is_refused():
    """
    Decorators apply bottom-up, so above it this would run after the routes were built. Refused
    rather than silently documenting nothing.
    """
    router = APIRouter()

    class TrackViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=DATABASE, pk_field="id")

    routed = route_viewset(router, base_path="/t", pk_field_name="id")(TrackViewSet)
    with pytest.raises(ValueError, match="must go below"):
        endpoint_docs(DOCS)(routed)


def test_documentation_is_inherited_and_overridable():
    class Base:
        pass

    endpoint_docs({"list_items": {"summary": "base", "description": "kept"}})(Base)

    class Derived(Base):
        pass

    endpoint_docs({"list_items": {"summary": "derived"}})(Derived)

    assert docs_for(Derived, "list_items") == {"summary": "derived", "description": "kept"}
