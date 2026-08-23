"""
Response shapes: what a viewset declares, what reaches the schema, and what a client can ask for.

The property worth pinning is the narrowing. A mixin has to declare every shape it could ever
produce, because it cannot know which viewset it will end up on; what reaches OpenAPI has to be
only the ones this viewset actually offers, or the schema documents an endpoint nobody wrote.
"""

import types

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from .collection_viewset import CollectionViewSet
from .conf import settings
from .decorators import route_viewset
from .list_shapes import ListOf, resolve_shapes, ShapeError
from .mixins import CursorListMixin, ListMixin, PaginatedListMixin


class Track(BaseModel):
    id: int = Field(json_schema_extra={"autoinc_int": True})
    title: str


DATABASE = {n: Track(id=n, title=f"Track {n:03d}") for n in range(1, 21)}


def client_for(base=ListMixin, **attributes) -> TestClient:
    app, router = FastAPI(), APIRouter()

    def body(namespace):
        namespace.update(attributes)
        namespace["schema"] = Track
        namespace["__init__"] = lambda self: CollectionViewSet.__init__(
            self,
            container=DATABASE,
            pk_field="id",
        )

    viewset = types.new_class(
        "V",
        (CollectionViewSet[int, Track], base[Track, None]),
        {},
        body,
    )
    route_viewset(router, base_path="/t", pk_field_name="id")(viewset)
    app.include_router(router)
    return TestClient(app)


def operation(client: TestClient) -> dict:
    return client.get("/t/schema").json()["paths"]["/t"]["get"]


def parameters(client: TestClient) -> set[str]:
    return {p["name"] for p in operation(client).get("parameters", [])}


def response_schema(client: TestClient) -> dict:
    return operation(client)["responses"]["200"]["content"]["application/json"]["schema"]


# ---------------------------------------------------------------------------
# resolve_shapes
# ---------------------------------------------------------------------------


def test_no_declaration_falls_through_to_the_setting():
    assert resolve_shapes(None, None, "cursor") == ("cursor", ("cursor",))


def test_declaring_one_shape_allows_only_that_one():
    """The common case, and the one that keeps a schema down to a single model."""
    assert resolve_shapes("paginated", None, "plain") == ("paginated", ("paginated",))


def test_the_default_is_always_first_and_always_allowed():
    """An endpoint that refused its own default would be a contradiction."""
    _, allowed = resolve_shapes("cursor", ("plain", "paginated"), "plain")
    assert allowed[0] == "cursor"
    assert set(allowed) == {"cursor", "plain", "paginated"}


def test_an_unknown_shape_is_refused_at_import_time():
    with pytest.raises(ShapeError, match="unknown list shape"):
        resolve_shapes("sideways", None, "plain")
    with pytest.raises(ShapeError, match="unknown list shape"):
        resolve_shapes("plain", ("plain", "diagonal"), "plain")


# ---------------------------------------------------------------------------
# What reaches the schema
# ---------------------------------------------------------------------------


def test_a_plain_viewset_advertises_no_paging_parameters():
    client = client_for()
    assert parameters(client) == {"fltr", "sort"}
    assert response_schema(client)["$ref"].endswith("/ListOf_Track_")


def test_a_cursor_viewset_advertises_a_cursor_and_no_offset():
    client = client_for(list_shape="cursor")
    assert {"cursor", "limit"} <= parameters(client)
    assert "offset" not in parameters(client)
    assert response_schema(client)["$ref"].endswith("/CursorPage_Track_")


def test_a_paginated_viewset_advertises_offset_and_no_cursor():
    client = client_for(list_shape="paginated")
    assert {"offset", "limit"} <= parameters(client)
    assert "cursor" not in parameters(client)
    assert response_schema(client)["$ref"].endswith("/PaginatedList_Track_")


def test_one_shape_means_no_header_at_all():
    """Offering a choice that does not exist is worse documentation than offering none."""
    assert "X-List-Shape" not in parameters(client_for(list_shape="cursor"))


def test_several_shapes_add_the_header_and_a_union():
    client = client_for(list_shape="cursor", list_shapes=("cursor", "plain"))
    assert "X-List-Shape" in parameters(client)
    members = {m["$ref"].rsplit("/", 1)[-1] for m in response_schema(client)["anyOf"]}
    assert members == {"CursorPage_Track_", "ListOf_Track_"}


def test_the_union_holds_only_the_declared_shapes():
    """The mixin declares all three; a viewset that offers two must not document the third."""
    client = client_for(list_shape="plain", list_shapes=("plain", "paginated"))
    members = {m["$ref"].rsplit("/", 1)[-1] for m in response_schema(client)["anyOf"]}
    assert "CursorPage_Track_" not in members


def test_the_header_names_the_default_in_its_description():
    client = client_for(list_shape="cursor", list_shapes=("cursor", "plain"))
    header = next(p for p in operation(client)["parameters"] if p["name"] == "X-List-Shape")
    assert "(cursor)" in header["description"]
    assert set(header["schema"]["enum"]) == {"cursor", "plain", None}


# ---------------------------------------------------------------------------
# What a request gets
# ---------------------------------------------------------------------------


def test_the_default_shape_is_what_arrives_without_a_header():
    assert isinstance(client_for(list_shape="cursor", list_shapes=("cursor", "plain")).get("/t").json(), dict)


def test_the_header_switches_the_envelope():
    client = client_for(list_shape="cursor", list_shapes=("cursor", "plain", "paginated"))
    assert isinstance(client.get("/t", headers={"X-List-Shape": "plain"}).json(), list)
    assert "offset" in client.get("/t", headers={"X-List-Shape": "paginated"}).json()
    assert "next" in client.get("/t", headers={"X-List-Shape": "cursor"}).json()


def test_a_shape_the_viewset_did_not_offer_is_refused():
    client = client_for(list_shape="cursor", list_shapes=("cursor", "plain"))
    assert client.get("/t", headers={"X-List-Shape": "paginated"}).status_code == 422


def test_the_global_setting_decides_when_a_viewset_does_not():
    settings.default_list_shape = "paginated"
    try:
        assert response_schema(client_for())["$ref"].endswith("/PaginatedList_Track_")
    finally:
        settings.default_list_shape = "plain"


# ---------------------------------------------------------------------------
# The shorthand mixins
# ---------------------------------------------------------------------------


def test_the_named_mixins_are_shorthand_for_a_shape():
    assert PaginatedListMixin.list_shape == "paginated"
    assert CursorListMixin.list_shape == "cursor"
    assert response_schema(client_for(base=CursorListMixin))["$ref"].endswith("/CursorPage_Track_")


def test_list_of_serialises_to_a_bare_array():
    """The wrapper exists for the schema; nothing about the wire format changes."""
    assert ListOf[Track]([Track(id=1, title="x")]).model_dump(mode="json") == [{"id": 1, "title": "x"}]
