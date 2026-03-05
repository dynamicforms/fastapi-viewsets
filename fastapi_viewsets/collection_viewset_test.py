import pytest

from pydantic import BaseModel, Field

from fastapi_viewsets.collection_viewset import CollectionViewSet


class MockItem:
    def __init__(self, id, name):
        self.id = id
        self.name = name

    def __eq__(self, other):
        return self.id == other.id and self.name == other.name

@pytest.mark.asyncio
async def test_collection_viewset_list_sequence():
    container = [
        {"id": 1, "name": "item1"},
        {"id": 2, "name": "item2"}
    ]
    viewset = CollectionViewSet(container)

    items = await viewset.perform_list()
    assert items == container
    assert items is not container # Should be a copy (list conversion)

@pytest.mark.asyncio
async def test_collection_viewset_retrieve_sequence():
    container = [
        {"id": 1, "name": "item1"},
        {"id": 2, "name": "item2"}
    ]
    viewset = CollectionViewSet(container)

    from fastapi import HTTPException
    item = await viewset.perform_retrieve(1)
    assert item == {"id": 1, "name": "item1"}

    with pytest.raises(HTTPException) as excinfo:
        await viewset.perform_retrieve(3)
    assert excinfo.value.status_code == 404

@pytest.mark.asyncio
async def test_collection_viewset_not_found_raises_http_exception():
    from fastapi import HTTPException
    container = [{"id": 1, "name": "item1"}]
    viewset = CollectionViewSet(container)

    # Test retrieve 404
    with pytest.raises(HTTPException) as excinfo:
        await viewset.perform_retrieve(2)
    assert excinfo.value.status_code == 404

    # Test update 404
    with pytest.raises(HTTPException) as excinfo:
        await viewset.perform_update(2, {"name": "new"}, partial=True)
    assert excinfo.value.status_code == 404

    # Test destroy 404
    with pytest.raises(HTTPException) as excinfo:
        await viewset.perform_destroy(2)
    assert excinfo.value.status_code == 404

    # Test mapping 404
    mapping_container = {1: {"id": 1, "name": "item1"}}
    mapping_viewset = CollectionViewSet(mapping_container)

    with pytest.raises(HTTPException) as excinfo:
        await mapping_viewset.perform_retrieve(2)
    assert excinfo.value.status_code == 404

    with pytest.raises(HTTPException) as excinfo:
        await mapping_viewset.perform_update(2, {"name": "new"}, partial=True)
    assert excinfo.value.status_code == 404

    with pytest.raises(HTTPException) as excinfo:
        await mapping_viewset.perform_destroy(2)
    assert excinfo.value.status_code == 404

@pytest.mark.asyncio
async def test_collection_viewset_create_sequence():
    container = []
    viewset = CollectionViewSet(container)

    new_item = {"id": 1, "name": "new"}
    await viewset.perform_create(new_item)

    assert container == [new_item]

@pytest.mark.asyncio
async def test_collection_viewset_update_sequence():
    container = [{"id": 1, "name": "old"}]
    viewset = CollectionViewSet(container)

    # Partial update
    await viewset.perform_update(1, {"name": "new"}, partial=True)
    assert container[0] == {"id": 1, "name": "new"}

    # Full update
    await viewset.perform_update(1, {"id": 1, "name": "full"}, partial=False)
    assert container[0] == {"id": 1, "name": "full"}

@pytest.mark.asyncio
async def test_collection_viewset_destroy_sequence():
    container = [{"id": 1, "name": "item1"}]
    viewset = CollectionViewSet(container)

    await viewset.perform_destroy(1)
    assert len(container) == 0

@pytest.mark.asyncio
async def test_collection_viewset_mapping():
    container = {
        1: {"id": 1, "name": "item1"},
        2: {"id": 2, "name": "item2"}
    }
    viewset = CollectionViewSet(container)

    items = await viewset.perform_list()
    assert len(items) == 2
    assert {"id": 1, "name": "item1"} in items

    item = await viewset.perform_retrieve(1)
    assert item == {"id": 1, "name": "item1"}

    await viewset.perform_create({"id": 3, "name": "item3"})
    assert 3 in container

    await viewset.perform_update(1, {"name": "updated"}, partial=True)
    assert container[1]["name"] == "updated"

    await viewset.perform_destroy(2)
    assert 2 not in container

@pytest.mark.asyncio
async def test_collection_viewset_immutable():
    container = ({"id": 1, "name": "item1"},)
    viewset = CollectionViewSet(container)

    assert (await viewset.perform_list()) == [{"id": 1, "name": "item1"}]
    assert (await viewset.perform_retrieve(1)) == {"id": 1, "name": "item1"}

    with pytest.raises(Exception, match="Provided container is not mutable"):
        await viewset.perform_create({"id": 2})

@pytest.mark.asyncio
async def test_collection_viewset_objects():
    item1 = MockItem(1, "item1")
    container = [item1]
    viewset = CollectionViewSet(container)

    assert (await viewset.perform_retrieve(1)) == item1

    await viewset.perform_update(1, {"name": "updated"}, partial=True)
    assert item1.name == "updated"

@pytest.mark.asyncio
async def test_collection_viewset_bulk():
    container = []
    viewset = CollectionViewSet(container)

    await viewset.perform_bulk_create([{"id": 1}, {"id": 2}])
    assert len(container) == 2

    await viewset.perform_bulk_update({1: {"name": "a"}, 2: {"name": "b"}}, partial=True)
    assert container[0]["name"] == "a"
    assert container[1]["name"] == "b"

    await viewset.perform_bulk_destroy([1, 2])
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
    await viewset.perform_create(item1)
    assert item1.id == 1
    assert container[1] == item1

    # Second element without ID
    item2 = Item(name="Second")
    await viewset.perform_create(item2)
    assert item2.id == 2
    assert container[2] == item2

    # Third element with manual ID (leave it alone)
    item5 = Item(id=5, name="Fifth")
    await viewset.perform_create(item5)
    assert item5.id == 5
    assert container[5] == item5

    # Next element without ID (must be 6)
    item6 = Item(name="Sixth")
    await viewset.perform_create(item6)
    assert item6.id == 6
    assert container[6] == item6
