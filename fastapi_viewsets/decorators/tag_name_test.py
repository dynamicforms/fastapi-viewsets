"""`settings.viewsets_tag_name`: the OpenAPI tag of a viewset's routes and everything that uses it."""

import re

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from .. import endpoint_docs
from ..collection_viewset import CollectionViewSet
from ..conf import settings
from ..endpoint_docs import apply_viewset_tags, viewset_tags
from ..mixins import ViewSetMixin
from ..mux_ws import registry
from . import route_viewset, ViewSetRouter


class Row(BaseModel):
    id: int
    name: str


def spaced(cls: type) -> str:
    """`ProfileEmailViewSet` -> `Profile Email`."""
    name = cls.__name__.removesuffix("ViewSet")
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


@pytest.fixture(autouse=True)
def _restore_tag_name(monkeypatch):
    monkeypatch.setattr(endpoint_docs, "_tag_descriptions", {})
    saved = settings.viewsets_tag_name
    yield
    settings.viewsets_tag_name = saved


def rows_app(base_path: str) -> tuple[FastAPI, type]:
    router = APIRouter()

    class RowEntriesViewSet(CollectionViewSet[int, Row], ViewSetMixin[int, Row]):
        """Entries of one row."""

        def __init__(self):
            super().__init__(container={1: Row(id=1, name="a")}, pk_field="id")

    @route_viewset(router, base_path, pk_field_name="id")
    class ProfileEmailViewSet(CollectionViewSet[int, Row], ViewSetMixin[int, Row]):
        """The e-mail addresses of a profile."""

        __router = ViewSetRouter()
        __router.sub_viewset("entries", RowEntriesViewSet, pk_field_name="id")

        def __init__(self):
            super().__init__(container={1: Row(id=1, name="a")}, pk_field="id")

    app = FastAPI()
    app.include_router(router)
    return app, ProfileEmailViewSet


def operation_tags(document: dict, prefix: str) -> set[str]:
    return {
        tag
        for path, item in document["paths"].items()
        if path.startswith(prefix)
        for operation in item.values()
        for tag in operation.get("tags", [])
    }


def test_without_the_setting_the_tag_is_the_class_name_without_viewset():
    app, _cls = rows_app("/default-emails")
    document = TestClient(app).get("/openapi.json").json()
    assert operation_tags(document, "/default-emails") == {"ProfileEmail"}


def test_every_consumer_of_the_tag_uses_the_name_the_setting_returns():
    settings.viewsets_tag_name = spaced
    app, cls = rows_app("/spaced-emails")
    client = TestClient(app)

    document = client.get("/openapi.json").json()
    # the viewset's own operations and those of its sub-viewset
    assert operation_tags(document, "/spaced-emails") == {"Profile Email"}
    assert operation_tags(document, "/spaced-emails/entries") == {"Profile Email"}

    # the tag description
    described = {tag["name"]: tag["description"] for tag in viewset_tags()}
    assert described["Profile Email"] == "The e-mail addresses of a profile."
    assert "ProfileEmail" not in described
    app = apply_viewset_tags(app)
    root_tags = {tag["name"]: tag["description"] for tag in client.get("/openapi.json").json()["tags"]}
    assert root_tags["Profile Email"] == "The e-mail addresses of a profile."

    # the viewset's /schema, its operations and its tag list
    schema = client.get("/spaced-emails/schema").json()
    assert operation_tags(schema, "/spaced-emails") == {"Profile Email"}
    assert any(tag["name"] == "Profile Email" for tag in schema["tags"])

    # the muxws registration and its schema
    assert registry._registrations[registry._key(cls)].tags == ["Profile Email"]
    assert operation_tags(registry.schema_for("/spaced-emails"), "/spaced-emails") == {"Profile Email"}


def test_an_empty_name_gives_no_tag():
    settings.viewsets_tag_name = lambda _cls: ""
    app, cls = rows_app("/untagged-emails")
    document = TestClient(app).get("/openapi.json").json()
    assert operation_tags(document, "/untagged-emails") == set()
    assert registry._registrations[registry._key(cls)].tags is None


def test_the_setting_is_read_when_the_viewset_is_decorated():
    settings.viewsets_tag_name = spaced
    app, _cls = rows_app("/decorated-emails")
    settings.viewsets_tag_name = lambda _cls: "Later"
    document = TestClient(app).get("/openapi.json").json()
    assert operation_tags(document, "/decorated-emails") == {"Profile Email"}
