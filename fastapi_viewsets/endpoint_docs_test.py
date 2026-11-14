"""Per-viewset documentation for endpoints a mixin provided."""

import pytest

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from .collection_viewset import CollectionViewSet
from .conf import settings
from .decorators import route_viewset
from .endpoint_docs import apply_viewset_tags, DOCS_ATTRIBUTE, docs_for, endpoint_docs, merge_openapi_extra
from .exceptions import DfViewSetError, NotFoundError
from .middleware.idempotency import Idempotency
from .mixins import BulkViewSetMixin
from .response_classes import error_entry, merge_responses, NOT_FOUND_RESPONSE


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


def default_param_client(docs=None) -> TestClient:
    from fastapi import Path, Query

    app, router = FastAPI(), APIRouter()

    @route_viewset(router, base_path="/p", pk_field_name="id")
    @endpoint_docs(docs or {})
    class DefaultParamViewSet(CollectionViewSet[int, Track]):
        __router = APIRouter()

        def __init__(self):
            super().__init__(container=DATABASE, pk_field="id")

        @__router.get("/{pk}/play")
        async def play(
            self,
            pk: int = Path(description="Track to play."),
            volume: int = Query(5, description="Loudness."),
            speed: float = Query(description="Playback speed."),
            note=Query("none", description="Free text."),  # noqa: B008 - an untyped parameter
        ) -> dict:
            return {"pk": pk, "volume": volume, "speed": speed, "note": note}

    app.include_router(router)
    return TestClient(app)


def test_parameters_declared_as_defaults_keep_their_description_and_default():
    client = default_param_client()
    operation = client.get("/p/schema").json()["paths"]["/p/{pk}/play"]["get"]
    pk, volume, speed = (parameter(operation, name) for name in ("pk", "volume", "speed"))
    assert (pk["description"], pk["required"]) == ("Track to play.", True)
    assert (volume["description"], volume["required"], volume["schema"]["default"]) == ("Loudness.", False, 5)
    assert (speed["description"], speed["required"]) == ("Playback speed.", True)
    assert parameter(operation, "note")["description"] == "Free text."
    assert client.get("/p/1/play?speed=1.5").json() == {"pk": 1, "volume": 5, "speed": 1.5, "note": "none"}
    assert client.get("/p/1/play").status_code == 422


def test_endpoint_docs_describe_parameters_declared_as_defaults():
    docs = {"play": {"parameters": {"pk": "Which track.", "volume": "How loud."}}}
    operation = default_param_client(docs).get("/p/schema").json()["paths"]["/p/{pk}/play"]["get"]
    assert parameter(operation, "pk")["description"] == "Which track."
    assert parameter(operation, "volume")["description"] == "How loud."


def test_derived_request_bodies_keep_the_models_description():
    class Album(BaseModel):
        """A released album."""

        id: int = Field(json_schema_extra={"autoinc_int": True})
        title: str

    app, router = FastAPI(), APIRouter()

    @route_viewset(router, base_path="/a", pk_field_name="id")
    class AlbumViewSet(CollectionViewSet[int, Album], BulkViewSetMixin[int, Album]):
        def __init__(self):
            super().__init__(container={}, pk_field="id")

    app.include_router(router)
    schemas = TestClient(app).get("/a/schema").json()["components"]["schemas"]
    assert {name: schemas[name].get("description") for name in ("Album", "AlbumNoPK", "AlbumPartial")} == {
        "Album": "A released album.",
        "AlbumNoPK": "A released album.",
        "AlbumPartial": "A released album.",
    }


def test_a_form_model_is_the_named_request_body():
    from typing import Annotated

    from fastapi import Form, UploadFile

    class TitledUpload(BaseModel):
        """A file with its title."""

        title: str = Field(description="Shown under the file.")
        file: UploadFile

    app, router = FastAPI(), APIRouter()

    @route_viewset(router, base_path="/u", pk_field_name="id")
    class UploadViewSet(CollectionViewSet[int, Track]):
        __router = APIRouter()

        def __init__(self):
            super().__init__(container=DATABASE, pk_field="id")

        @__router.post("/titled")
        async def titled(self, data: Annotated[TitledUpload, Form()]) -> dict:
            return {"title": data.title, "content": (await data.file.read()).hex()}

    app.include_router(router)
    client = TestClient(app)
    response = client.post("/u/titled", data={"title": "Logo"}, files={"file": ("logo.png", b"\x89PNG", "image/png")})
    assert response.json() == {"title": "Logo", "content": "89504e47"}
    document = client.get("/u/schema").json()
    content = document["paths"]["/u/titled"]["post"]["requestBody"]["content"]
    assert [media["schema"] for media in content.values()] == [{"$ref": "#/components/schemas/TitledUpload"}]
    assert document["components"]["schemas"]["TitledUpload"]["description"] == "A file with its title."


# responses, openapi_extra and '*'


class TrackConflictError(DfViewSetError):
    def __init__(self, pk: int):
        super().__init__(409, f"Track {pk} is being edited", "app.track_conflict", {"pk": pk})


def app_document(docs) -> tuple[dict, dict]:
    """The application's OpenAPI document and the viewset's `/schema`."""
    client = documented_client(docs)
    return client.app.openapi(), client.get("/t/schema").json()


def media(response: dict) -> dict:
    return response["content"]["application/json"]


def test_responses_are_merged_with_the_package_and_mixin_entries():
    document, _ = app_document(
        {
            "retrieve": {
                "responses": merge_responses(
                    error_entry(409, "The track is being edited: `app.track_conflict`.", TrackConflictError(1)),
                    error_entry(404, "Or archived.", archived=NotFoundError(7)),
                    error_entry(422, "Or a pk out of range.", out_of_range=HTTPException(422, "Out of range")),
                )
            }
        }
    )
    responses = document["paths"]["/t/{pk}"]["get"]["responses"]
    assert {"200", "404", "409", "422", "500", "default"} <= set(responses)
    assert set(media(responses["409"])["examples"]) == {"track_conflict"}
    assert set(media(responses["404"])["examples"]) == {"not_found", "archived"}
    assert responses["404"]["description"] == f"{NOT_FOUND_RESPONSE['404']['description']}\n\nOr archived."
    assert set(media(responses["422"])["examples"]) == {"validation_failed", "idempotency_key_reused", "out_of_range"}
    assert media(responses["409"])["schema"] == {"$ref": "#/components/schemas/ApiErrorBody"}


def test_responses_of_one_action_leave_the_others_alone():
    document, _ = app_document({"retrieve": {"responses": error_entry(409, "Edited.", TrackConflictError(1))}})
    assert "409" not in document["paths"]["/t/{pk}"]["put"]["responses"]
    assert set(media(document["paths"]["/t/{pk}"]["put"]["responses"]["404"])["examples"]) == {"not_found"}


def test_openapi_extra_is_merged_with_the_middleware_parameters(monkeypatch):
    monkeypatch.setattr(settings, "viewsets_command_middleware", [Idempotency()])
    tenant = {"name": "X-Tenant", "in": "header", "required": True, "schema": {"type": "string"}}
    document, schema = app_document(
        {
            "*": {"openapi_extra": {"security": [{"session": []}], "parameters": [tenant]}},
            "retrieve": {"openapi_extra": {"security": [], "x-audit": {"level": "low"}}},
        }
    )
    for doc in (document, schema):
        retrieve = doc["paths"]["/t/{pk}"]["get"]
        names = [parameter["name"] for parameter in retrieve["parameters"]]
        assert names == ["pk", "X-Tenant", "Idempotency-Key"]
        assert retrieve["security"] == []
        assert retrieve["x-audit"] == {"level": "low"}
        assert retrieve["x-viewset-action"] == "retrieve"
        create = doc["paths"]["/t"]["post"]
        assert create["security"] == [{"session": []}]
        assert create["requestBody"]["content"]


def test_declared_security_replaces_that_of_the_security_scheme(monkeypatch):
    from fastapi.security import APIKeyCookie

    monkeypatch.setattr(settings, "viewsets_security_scheme", APIKeyCookie(name="sessionid", auto_error=False))
    document, schema = app_document(
        {
            "*": {"openapi_extra": {"security": [{"session": []}]}},
            "retrieve": {"openapi_extra": {"security": []}},
        }
    )
    for doc in (document, schema):
        assert doc["paths"]["/t/{pk}"]["get"]["security"] == []
        assert doc["paths"]["/t"]["get"]["security"] == [{"session": []}]
        assert doc["paths"]["/t/{pk}"]["put"]["security"] == [{"session": []}]
        assert doc["paths"]["/t/schema"]["get"]["security"] == [{"APIKeyCookie": []}]
        assert "APIKeyCookie" in doc["components"]["securitySchemes"]


def test_openapi_extra_merges_into_the_mixins_own():
    document, _ = app_document({"bulk_create": {"openapi_extra": {"requestBody": {"required": True}}}})
    body = document["paths"]["/t/bulk"]["post"]["requestBody"]
    assert body["required"] is True
    assert body["description"] == "The records to create."


def test_the_every_action_entry_documents_every_action_but_schema():
    document, _ = app_document({"*": {"deprecated": True, "tags": ["Catalogue"]}})
    for path, operations in document["paths"].items():
        for operation in operations.values():
            if path == "/t/schema":
                assert "deprecated" not in operation
                assert operation["tags"] != ["Catalogue"]
            else:
                assert operation["deprecated"] is True
                assert operation["tags"] == ["Catalogue"]


def test_the_action_entry_overrides_the_every_action_entry():
    document, _ = app_document(
        {
            "*": {"description": "Needs a session.", "responses": error_entry(409, "Edited.", TrackConflictError(1))},
            "retrieve": {
                "description": "One track.",
                "responses": error_entry(409, "Locked.", locked=TrackConflictError(2)),
            },
        }
    )
    retrieve = document["paths"]["/t/{pk}"]["get"]
    assert retrieve["description"] == "One track."
    assert retrieve["responses"]["409"]["description"] == "Edited.\n\nLocked."
    assert set(media(retrieve["responses"]["409"])["examples"]) == {"track_conflict", "locked"}
    assert document["paths"]["/t"]["get"]["description"] == "Needs a session."


def test_the_every_action_entry_is_merged_before_the_action_along_the_mro():
    class Base:
        pass

    class Derived(Base):
        pass

    endpoint_docs(
        {
            "*": {"summary": "base every", "description": "base every", "openapi_extra": {"parameters": [{"n": 1}]}},
            "retrieve": {"summary": "base retrieve", "openapi_extra": {"parameters": [{"n": 2}]}},
        }
    )(Base)
    endpoint_docs({"*": {"description": "derived every", "openapi_extra": {"parameters": [{"n": 3}]}}})(Derived)
    assert docs_for(Derived, "retrieve") == {
        "summary": "base retrieve",
        "description": "derived every",
        "openapi_extra": {"parameters": [{"n": 1}, {"n": 2}, {"n": 3}]},
    }
    assert docs_for(Derived, "schema") == {}


def test_a_parameter_of_the_every_action_entry_describes_only_the_endpoints_that_have_it():
    document, _ = app_document({"*": {"parameters": {"pk": "Catalogue number."}}})
    parameters = document["paths"]["/t/{pk}"]["get"]["parameters"]
    assert parameters[0]["description"] == "Catalogue number."
    assert "pk" not in [parameter["name"] for parameter in document["paths"]["/t"]["post"].get("parameters", [])]


def test_a_key_outside_endpoint_doc_is_refused():
    with pytest.raises(ValueError, match="response"):
        endpoint_docs({"retrieve": {"response": {}}})
    with pytest.raises(ValueError, match="operation_id"):
        endpoint_docs({"*": {"operation_id": "x"}})


def test_tags_and_openapi_extra_reach_the_schema_endpoint():
    docs = {
        "list_items": {"tags": ["Catalogue"], "openapi_extra": {"x-audit": True}},
        "retrieve": {"responses": error_entry(409, "Edited.", TrackConflictError(1))},
    }
    document, schema = app_document(docs)
    for path, method in (("/t", "get"), ("/t/{pk}", "get"), ("/t/{pk}", "delete")):
        assert schema["paths"][path][method] == document["paths"][path][method]
    assert schema["paths"]["/t"]["get"]["tags"] == ["Catalogue"]
    assert schema["paths"]["/t"]["get"]["x-audit"] is True


def test_a_library_routed_viewset_is_documented_through_its_class(monkeypatch):
    from .events.viewset import EventsViewSet, register_events

    monkeypatch.setattr(EventsViewSet, DOCS_ATTRIBUTE, {}, raising=False)
    endpoint_docs({"*": {"openapi_extra": {"security": []}}, "poll": {"summary": "Poll the topics"}})(EventsViewSet)
    app, router = FastAPI(), APIRouter()
    register_events(router)
    app.include_router(router)
    poll = app.openapi()["paths"]["/events/poll"]["post"]
    assert poll["summary"] == "Poll the topics"
    assert poll["security"] == []


def test_merge_openapi_extra_merges_dicts_concatenates_parameters_and_replaces_the_rest():
    base = {"parameters": [{"n": 1}], "requestBody": {"description": "d", "content": {"a": 1}}, "security": [{"a": []}]}
    extra = {"parameters": [{"n": 2}], "requestBody": {"required": True, "content": {"b": 2}}, "security": []}
    assert merge_openapi_extra(base, extra) == {
        "parameters": [{"n": 1}, {"n": 2}],
        "requestBody": {"description": "d", "content": {"a": 1, "b": 2}, "required": True},
        "security": [],
    }
    assert base["parameters"] == [{"n": 1}]
    assert merge_openapi_extra(None, None) == {}
