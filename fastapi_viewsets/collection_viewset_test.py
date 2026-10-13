import pytest

from pydantic import BaseModel, Field, field_validator

from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.exceptions import ReadOnlyCollectionError

CTX = {}


class MockItem:
    def __init__(self, id, name):
        self.id = id
        self.name = name

    def __eq__(self, other):
        return self.id == other.id and self.name == other.name


@pytest.mark.asyncio
async def test_collection_viewset_list_sequence():
    container = [{"id": 1, "name": "item1"}, {"id": 2, "name": "item2"}]
    viewset = CollectionViewSet(container)

    items = await viewset.perform_list(CTX)
    assert items == container
    assert items is not container  # Should be a copy (list conversion)


@pytest.mark.asyncio
async def test_collection_viewset_retrieve_sequence():
    container = [{"id": 1, "name": "item1"}, {"id": 2, "name": "item2"}]
    viewset = CollectionViewSet(container)

    from fastapi import HTTPException

    item = await viewset.perform_retrieve(CTX, 1)
    assert item == {"id": 1, "name": "item1"}

    with pytest.raises(HTTPException) as excinfo:
        await viewset.perform_retrieve(CTX, 3)
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_collection_viewset_not_found_raises_http_exception():
    from fastapi import HTTPException

    container = [{"id": 1, "name": "item1"}]
    viewset = CollectionViewSet(container)

    # Test retrieve 404
    with pytest.raises(HTTPException) as excinfo:
        await viewset.perform_retrieve(CTX, 2)
    assert excinfo.value.status_code == 404

    # Test update 404
    with pytest.raises(HTTPException) as excinfo:
        await viewset.perform_update(CTX, 2, {"name": "new"}, partial=True)
    assert excinfo.value.status_code == 404

    # Test destroy 404
    with pytest.raises(HTTPException) as excinfo:
        await viewset.perform_destroy(CTX, 2)
    assert excinfo.value.status_code == 404

    # Test mapping 404
    mapping_container = {1: {"id": 1, "name": "item1"}}
    mapping_viewset = CollectionViewSet(mapping_container)

    with pytest.raises(HTTPException) as excinfo:
        await mapping_viewset.perform_retrieve(CTX, 2)
    assert excinfo.value.status_code == 404

    with pytest.raises(HTTPException) as excinfo:
        await mapping_viewset.perform_update(CTX, 2, {"name": "new"}, partial=True)
    assert excinfo.value.status_code == 404

    with pytest.raises(HTTPException) as excinfo:
        await mapping_viewset.perform_destroy(CTX, 2)
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_collection_viewset_create_sequence():
    container = []
    viewset = CollectionViewSet(container)

    new_item = {"id": 1, "name": "new"}
    await viewset.perform_create(CTX, new_item)

    assert container == [new_item]


@pytest.mark.asyncio
async def test_collection_viewset_update_sequence():
    container = [{"id": 1, "name": "old"}]
    viewset = CollectionViewSet(container)

    # Partial update
    await viewset.perform_update(CTX, 1, {"name": "new"}, partial=True)
    assert container[0] == {"id": 1, "name": "new"}

    # Full update
    await viewset.perform_update(CTX, 1, {"id": 1, "name": "full"}, partial=False)
    assert container[0] == {"id": 1, "name": "full"}


@pytest.mark.asyncio
async def test_collection_viewset_destroy_sequence():
    container = [{"id": 1, "name": "item1"}]
    viewset = CollectionViewSet(container)

    await viewset.perform_destroy(CTX, 1)
    assert len(container) == 0


@pytest.mark.asyncio
async def test_collection_viewset_mapping():
    container = {1: {"id": 1, "name": "item1"}, 2: {"id": 2, "name": "item2"}}
    viewset = CollectionViewSet(container)

    items = await viewset.perform_list(CTX)
    assert len(items) == 2
    assert {"id": 1, "name": "item1"} in items

    item = await viewset.perform_retrieve(CTX, 1)
    assert item == {"id": 1, "name": "item1"}

    await viewset.perform_create(CTX, {"id": 3, "name": "item3"})
    assert 3 in container

    await viewset.perform_update(CTX, 1, {"name": "updated"}, partial=True)
    assert container[1]["name"] == "updated"

    await viewset.perform_destroy(CTX, 2)
    assert 2 not in container


@pytest.mark.asyncio
async def test_collection_viewset_immutable():
    container = ({"id": 1, "name": "item1"},)
    viewset = CollectionViewSet(container)

    assert (await viewset.perform_list(CTX)) == [{"id": 1, "name": "item1"}]
    assert (await viewset.perform_retrieve(CTX, 1)) == {"id": 1, "name": "item1"}

    with pytest.raises(ReadOnlyCollectionError) as exc_info:
        await viewset.perform_create(CTX, {"id": 2})
    assert exc_info.value.status_code == 405
    assert exc_info.value.code == "read_only"


@pytest.mark.asyncio
async def test_collection_viewset_objects():
    item1 = MockItem(1, "item1")
    container = [item1]
    viewset = CollectionViewSet(container)

    assert (await viewset.perform_retrieve(CTX, 1)) == item1

    await viewset.perform_update(CTX, 1, {"name": "updated"}, partial=True)
    assert item1.name == "updated"


@pytest.mark.asyncio
async def test_collection_viewset_bulk():
    container = []
    viewset = CollectionViewSet(container)

    await viewset.perform_bulk_create(CTX, [{"id": 1}, {"id": 2}])
    assert len(container) == 2

    await viewset.perform_bulk_update(CTX, {1: {"name": "a"}, 2: {"name": "b"}}, partial=True)
    assert container[0]["name"] == "a"
    assert container[1]["name"] == "b"

    await viewset.perform_bulk_destroy(CTX, [1, 2])
    assert len(container) == 0


@pytest.mark.asyncio
async def test_collection_viewset_autoinc():
    class Item(BaseModel):
        id: int | None = Field(default=None, json_schema_extra={"autoinc_int": True})
        name: str

    container = {}
    viewset = CollectionViewSet(container, pk_field="id")

    # First element without ID
    item1 = Item(name="First")
    await viewset.perform_create(CTX, item1)
    assert item1.id == 1
    assert container[1] == item1

    # Second element without ID
    item2 = Item(name="Second")
    await viewset.perform_create(CTX, item2)
    assert item2.id == 2
    assert container[2] == item2

    # Third element with manual ID (leave it alone)
    item5 = Item(id=5, name="Fifth")
    await viewset.perform_create(CTX, item5)
    assert item5.id == 5
    assert container[5] == item5

    # Next element without ID (must be 6)
    item6 = Item(name="Sixth")
    await viewset.perform_create(CTX, item6)
    assert item6.id == 6
    assert container[6] == item6


# ---------------------------------------------------------------------------
# Partial updates and sets, through the routes
# ---------------------------------------------------------------------------


class Track(BaseModel):
    id: int
    title: str = Field(min_length=1)
    rating: int = 0


class FrozenTrack(Track, frozen=True):
    pass


def _client(container, model):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from fastapi_viewsets.decorators import route_viewset
    from fastapi_viewsets.exceptions import df_viewset_exception_handler, DfViewSetError
    from fastapi_viewsets.mixins import BulkViewSetMixin

    router = APIRouter()

    @route_viewset(router, base_path="/tracks", pk_field_name="id")
    class TrackViewSet(CollectionViewSet[int, model], BulkViewSetMixin[int, model]):
        def __init__(self):
            super().__init__(container=container, pk_field="id")

    app = FastAPI()
    app.add_exception_handler(DfViewSetError, df_viewset_exception_handler)
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize(
    "container",
    [
        pytest.param(lambda: [Track(id=1, title="One", rating=3)], id="list"),
        pytest.param(lambda: {1: Track(id=1, title="One", rating=3)}, id="dict"),
    ],
)
def test_patch_writes_only_the_fields_it_states(container):
    client = _client(container(), Track)
    response = client.patch("/tracks/1", json={"rating": 5})
    assert response.status_code == 200
    assert response.json() == {"id": 1, "title": "One", "rating": 5}
    assert client.get("/tracks/1").json() == {"id": 1, "title": "One", "rating": 5}


def test_patch_validates_the_fields_it_states():
    client = _client([Track(id=1, title="One")], Track)
    response = client.patch("/tracks/1", json={"title": ""})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "title"]


def test_bulk_patch_writes_only_the_fields_each_record_states():
    client = _client([Track(id=1, title="One", rating=1), Track(id=2, title="Two", rating=2)], Track)
    response = client.patch("/tracks/bulk", json={"1": {"rating": 9}, "2": {"title": "Deux"}})
    assert response.status_code == 200
    assert response.json() == [{"id": 1, "title": "One", "rating": 9}, {"id": 2, "title": "Deux", "rating": 2}]


class Address(BaseModel):
    city: str
    zip: str = "1000"


class Venue(BaseModel):
    id: int = 0
    name: str
    tags: list[str] = Field(default_factory=list)
    address: Address | None = None

    @field_validator("name")
    @classmethod
    def name_is_not_reserved(cls, value: str) -> str:
        if value == "reserved":
            raise ValueError("reserved name")
        return value


def test_patch_with_a_default_factory_field_writes_only_the_fields_it_states():
    client = _client([Venue(id=1, name="Hall", tags=["big"])], Venue)
    response = client.patch("/tracks/1", json={"name": "Arena"})
    assert response.json() == {"id": 1, "name": "Arena", "tags": ["big"], "address": None}


def test_patch_stores_a_nested_model_as_the_model():
    container = [Venue(id=1, name="Hall", address=Address(city="Kranj"))]
    client = _client(container, Venue)
    response = client.patch("/tracks/1", json={"address": {"city": "Bled"}})
    assert response.json()["address"] == {"city": "Bled", "zip": "1000"}
    assert isinstance(container[0].address, Address)


def test_patch_applies_the_models_field_validators():
    client = _client([Venue(id=1, name="Hall")], Venue)
    response = client.patch("/tracks/1", json={"name": "reserved"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "name"]


def test_put_still_replaces_the_whole_record():
    client = _client([Track(id=1, title="One", rating=3)], Track)
    response = client.put("/tracks/1", json={"id": 1, "title": "Uno"})
    assert response.json() == {"id": 1, "title": "Uno", "rating": 0}


def test_a_set_updates_partially_and_deletes():
    container = {FrozenTrack(id=1, title="One", rating=3), FrozenTrack(id=2, title="Two")}
    client = _client(container, FrozenTrack)

    assert client.patch("/tracks/1", json={"rating": 5}).json() == {"id": 1, "title": "One", "rating": 5}
    assert client.delete("/tracks/2").status_code == 200
    assert {track.id for track in container} == {1}
    assert client.delete("/tracks/2").status_code == 404


def test_a_write_to_an_immutable_container_is_405():
    client = _client((Track(id=1, title="One"),), Track)
    response = client.post("/tracks", json={"title": "Two"})
    assert response.status_code == 405
    assert response.json()["detail_code"] == "read_only"
