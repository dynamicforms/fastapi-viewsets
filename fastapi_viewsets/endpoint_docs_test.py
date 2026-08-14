"""Per-viewset documentation for endpoints a mixin provided."""

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from .collection_viewset import CollectionViewSet
from .decorators import route_viewset
from .endpoint_docs import apply_viewset_tags, docs_for, endpoint_docs
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


# ---------------------------------------------------------------------------
# Viewset-level description
# ---------------------------------------------------------------------------

def test_a_viewsets_docstring_describes_its_group():
    """
    However well each endpoint is documented, the section they sit in was blank - and that intro is
    the first thing a reader of the API reference meets.
    """
    router = APIRouter()

    @route_viewset(router, base_path="/described", pk_field_name="id")
    class DescribedViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        """The catalogue, and what you can do to it."""

        def __init__(self):
            super().__init__(container=DATABASE, pk_field="id")

    app = apply_viewset_tags(FastAPI())
    described = {tag["name"]: tag["description"] for tag in app.openapi_tags}
    assert described["Described"] == "The catalogue, and what you can do to it."


def test_a_viewset_that_says_nothing_stays_silent():
    """Inheriting a mixin's "List a queryset" would look deliberate, which is worse than blank."""
    router = APIRouter()

    @route_viewset(router, base_path="/silent", pk_field_name="id")
    class SilentViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        def __init__(self):
            super().__init__(container=DATABASE, pk_field="id")

    app = apply_viewset_tags(FastAPI())
    assert "Silent" not in {tag["name"] for tag in app.openapi_tags}


def test_the_application_can_override_a_viewsets_description():
    router = APIRouter()

    @route_viewset(router, base_path="/overridden", pk_field_name="id")
    class OverriddenViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        """What the library says."""

        def __init__(self):
            super().__init__(container=DATABASE, pk_field="id")

    app = apply_viewset_tags(
        FastAPI(), extra=[{"name": "Overridden", "description": "What the application says."}],
    )
    described = {tag["name"]: tag["description"] for tag in app.openapi_tags}
    assert described["Overridden"] == "What the application says."


def test_the_description_reaches_the_per_viewset_schema_without_any_wiring():
    """/schema is served by the library's own app, so it should not need apply_viewset_tags."""
    app, router = FastAPI(), APIRouter()

    @route_viewset(router, base_path="/wired", pk_field_name="id")
    class WiredViewSet(CollectionViewSet[int, Track], BulkViewSetMixin[int, Track]):
        """Documented without the application lifting a finger."""

        def __init__(self):
            super().__init__(container=DATABASE, pk_field="id")

    app.include_router(router)
    tags = TestClient(app).get("/wired/schema").json().get("tags", [])
    assert any(t["name"] == "Wired" and "lifting a finger" in t["description"] for t in tags)
