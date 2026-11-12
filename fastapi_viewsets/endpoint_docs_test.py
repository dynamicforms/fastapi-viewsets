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
    Two routers are built from one declaration - the app's and the one /schema reports on - and
    both carry the endpoint docs.
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
        FastAPI(),
        extra=[{"name": "Overridden", "description": "What the application says."}],
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


# ---------------------------------------------------------------------------
# Parameters and the /schema endpoint
# ---------------------------------------------------------------------------


def parameter(operation: dict, name: str) -> dict:
    return next(p for p in operation["parameters"] if p["name"] == name)


def test_parameter_descriptions_merge_per_parameter_along_the_mro():
    class Base:
        pass

    endpoint_docs({"retrieve": {"parameters": {"pk": "base", "other": "kept"}}})(Base)

    class Derived(Base):
        pass

    endpoint_docs({"retrieve": {"summary": "s", "parameters": {"pk": "derived"}}})(Derived)

    assert docs_for(Derived, "retrieve") == {"summary": "s", "parameters": {"pk": "derived", "other": "kept"}}


def test_the_pk_is_described_by_default():
    assert (
        parameter(paths(documented_client({}))["/t/{pk}"]["get"], "pk")["description"] == "Primary key of the record."
    )


def test_the_pk_takes_the_description_of_the_models_primary_key_field():
    class Described(BaseModel):
        id: int = Field(description="Catalogue number.")
        title: str

    app, router = FastAPI(), APIRouter()

    @route_viewset(router, base_path="/d", pk_field_name="id")
    class DescribedViewSet(CollectionViewSet[int, Described], BulkViewSetMixin[int, Described]):
        def __init__(self):
            super().__init__(container={}, pk_field="id")

    app.include_router(router)
    operations = TestClient(app).get("/d/schema").json()["paths"]["/d/{pk}"]
    assert {parameter(operation, "pk")["description"] for operation in operations.values()} == {"Catalogue number."}


def test_endpoint_docs_describe_parameters_by_their_schema_name():
    docs = {
        "retrieve": {"parameters": {"pk": "Track number."}},
        "list_items": {"parameters": {"sort": "Order of the tracks."}},
    }
    documented = paths(documented_client(docs))
    assert parameter(documented["/t/{pk}"]["get"], "pk")["description"] == "Track number."
    assert parameter(documented["/t"]["get"], "sort")["description"] == "Order of the tracks."
    # other actions keep the default
    assert parameter(documented["/t/{pk}"]["delete"], "pk")["description"] == "Primary key of the record."


def test_describing_a_parameter_the_endpoint_does_not_have_is_refused():
    with pytest.raises(ValueError, match="describes parameter.*nope.*it has pk"):
        documented_client({"retrieve": {"parameters": {"nope": "x"}}})


def test_a_list_without_a_filter_model_publishes_no_filter_parameter():
    names = [p["name"] for p in paths(documented_client({}))["/t"]["get"]["parameters"]]
    assert "fltr" not in names


def test_the_list_without_a_filter_model_still_answers():
    client = documented_client({})
    assert client.get("/t").status_code == 200


def test_the_schema_endpoint_answers_a_named_openapi_document():
    operation = paths(documented_client({}))["/t/schema"]["get"]
    assert operation["description"].startswith("OpenAPI document of this viewset's endpoints")
    content = operation["responses"]["200"]["content"]["application/json"]
    assert content["schema"] == {"$ref": "#/components/schemas/OpenAPIDocument"}


def test_the_schema_endpoint_is_documented_through_endpoint_docs():
    operation = paths(documented_client({"schema": {"description": "For the TS client."}}))["/t/schema"]["get"]
    assert operation["description"] == "For the TS client."


def test_a_custom_endpoint_keeps_its_own_pk_description():
    from typing import Annotated

    from fastapi import Path

    app, router = FastAPI(), APIRouter()

    @route_viewset(router, base_path="/c", pk_field_name="id")
    class CustomViewSet(CollectionViewSet[int, Track]):
        __router = APIRouter()

        def __init__(self):
            super().__init__(container=DATABASE, pk_field="id")

        @__router.get("/{pk}/play")
        async def play(self, pk: Annotated[int, Path(description="Track to play.")]) -> int:
            return pk

    app.include_router(router)
    operation = TestClient(app).get("/c/schema").json()["paths"]["/c/{pk}/play"]["get"]
    assert parameter(operation, "pk")["description"] == "Track to play."
