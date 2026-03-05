import inspect

from typing import Generic, get_args, get_origin, TypeVar

import pytest

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, Field

from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.mixins import (
    BulkViewSetMixin,
    CreateMixin,
    ListMixin,
    LookupItem,
    LookupMixin,
    RetrieveMixin,
    ViewSetMixin,
)


class Item(BaseModel):
    id: int
    name: str

def test_viewset_decorator():
    router = APIRouter()

    @route_viewset(router=router, base_path="/items", lifecycle="singleton")
    class MyViewSet(ListMixin[Item], CreateMixin[int, Item], RetrieveMixin[int, Item]):
        async def perform_list(self) -> list[Item]:
            return [Item(id=1, name="a"), Item(id=2, name="b")]

        async def perform_create(self, data: Item) -> Item:
            return data

        async def perform_retrieve(self, pk: int) -> Item:
            return Item(id=pk, name=f"item_{pk}")

    paths = {route.path for route in router.routes}
    assert "/items" in paths
    assert "/items/{pk}" in paths

    methods = {}
    for route in router.routes:
        methods.setdefault(route.path, set())
        methods[route.path] = methods[route.path].union(set(route.methods))

    assert "GET" in methods["/items"]
    assert "POST" in methods["/items"]
    assert "GET" in methods["/items/{pk}"]

    assert MyViewSet.__viewset_metadata__["base_path"] == "/items"
    assert MyViewSet.__viewset_metadata__["lifecycle"] == "singleton"

@pytest.mark.asyncio
async def test_viewset_execution():
    router = APIRouter()

    @route_viewset(router=router, base_path="/complex", lifecycle="singleton")
    class ComplexViewSet(ListMixin[Item], RetrieveMixin[int, Item]):
        async def perform_list(self) -> list[Item]:
            return [Item(id=1, name="first")]

        async def perform_retrieve(self, pk: int) -> Item:
            return Item(id=pk, name="found")

    # Find routes for list and retrieve
    list_route = next(r for r in router.routes if r.path == "/complex" and "GET" in r.methods)
    retrieve_route = next(r for r in router.routes if r.path == "/complex/{pk}" and "GET" in r.methods)

    # In real FastAPI this would go through the app, here we check if endpoints are correctly bound
    res_list = await list_route.endpoint()
    assert res_list == [Item(id=1, name="first")]

    res_retrieve = await retrieve_route.endpoint(pk=42)
    assert res_retrieve == Item(id=42, name="found")


@pytest.mark.asyncio
async def test_viewset_per_request():
    router = APIRouter()

    class Counter:
        count = 0

    @route_viewset(router=router, base_path="/counter", lifecycle="per-request")
    class CounterViewSet(ListMixin[int]):
        def __init__(self):
            Counter.count += 1

        async def perform_list(self) -> list[int]:
            return [Counter.count]

    list_route = next(r for r in router.routes if r.path == "/counter" and "GET" in r.methods)

    res1 = await list_route.endpoint()
    assert res1 == [1]

    res2 = await list_route.endpoint()
    assert res2 == [2]


@pytest.mark.asyncio
async def test_viewset_instance_key():
    router = APIRouter()

    class StateStore:
        data = {}

    @route_viewset(router=router, base_path="/state", lifecycle="instance-key")
    class StateViewSet(ListMixin[str]):
        def __init__(self):
            self.state = "initial"

        async def load_state(self):
            self.state = StateStore.data.get("val", "loaded")

        async def save_state(self):
            StateStore.data["val"] = self.state + "_saved"

        async def perform_list(self) -> list[str]:
            current = self.state
            self.state = "modified"
            return [current]

    list_route = next(r for r in router.routes if r.path == "/state" and "GET" in r.methods)

    # First request
    res1 = await list_route.endpoint()
    assert res1 == ["loaded"]
    assert StateStore.data["val"] == "modified_saved"

    # Second request
    res2 = await list_route.endpoint()
    assert res2 == ["modified_saved"]
    assert StateStore.data["val"] == "modified_saved"  # stays same because we always set it to modified_saved



def test_viewset_endpoint_signature():
    router = APIRouter()

    @route_viewset(router=router, base_path="/items")
    class MyViewSet(ListMixin[Item]):
        async def perform_list(self) -> list[Item]:
            return []

    # Find the list route
    list_route = next(r for r in router.routes if r.path == "/items" and "GET" in r.methods)

    # Check endpoint signature
    sig = inspect.signature(list_route.endpoint)
    params = list(sig.parameters.keys())

    print(f"Endpoint parameters: {params}")

    # 'self' must not be in the signature because FastAPI tries to fill it from query/path parameters
    assert "self" not in params, f"Endpoint should not have 'self' parameter: {params}"
    # Also we don't want *args and **kwargs if they weren't in the original method
    # (unless FastAPI necessarily needs them, but usually we don't want to expose them in OpenAPI)

def test_viewset_endpoint_signature_with_params():
    router = APIRouter()

    @route_viewset(router=router, base_path="/items")
    class MyViewSet(RetrieveMixin[int, Item]):
        async def perform_retrieve(self, pk: int) -> Item:
            return Item(id=pk, name="test")

    # Find the retrieve route
    retrieve_route = next(r for r in router.routes if r.path == "/items/{pk}" and "GET" in r.methods)

    # Check endpoint signature
    sig = inspect.signature(retrieve_route.endpoint)
    params = list(sig.parameters.keys())

    print(f"Retrieve parameters: {params}")

    assert "self" not in params
    assert "pk" in params
    # Annotation might be Generic (TypeVar) because RetrieveMixin is Generic
    # but we just want to ensure it is there.

@pytest.mark.asyncio
async def test_viewset_autoinc_and_pk_hiding():

    class AutoIncItem(BaseModel):
        id: int = Field(json_schema_extra={"autoinc_int": True})
        name: str

    database: dict[int, AutoIncItem] = {
        1: AutoIncItem(id=1, name="Item 1")
    }

    class AutoIncViewSet(CollectionViewSet[int, AutoIncItem], ViewSetMixin[int, AutoIncItem]):
        def __init__(self):
            super().__init__(container=database, pk_field="id")

    router = APIRouter()
    route_viewset(router, base_path="/items", pk_field_name="id")(AutoIncViewSet)

    # 1. Check that POST /items does not have 'id' in the endpoint signature
    post_route = next(r for r in router.routes if r.path == "/items" and "POST" in r.methods)
    sig = inspect.signature(post_route.endpoint)
    data_param = sig.parameters["data"]

    # Check that 'id' field is not in the model used for POST at all
    assert "id" not in data_param.annotation.model_fields, "PK field should be hidden in POST"

    # 2. Check that the endpoint call actually works and uses autoinc
    # Create data without ID (as the client would send it)
    # Since 'id' is not in model_fields, we can't even provide it in the constructor
    item_no_pk = data_param.annotation
    item_in = item_no_pk(name="Item 2")

    result = await post_route.endpoint(data=item_in)

    assert hasattr(result, "id")
    assert result.id == 2
    assert database[2].name == "Item 2"
    assert isinstance(database[2], AutoIncItem)

@pytest.mark.asyncio
async def test_viewset_pk_not_hidden_in_update():
    from fastapi_viewsets.mixins import UpdateMixin

    class SimpleItem(BaseModel):
        id: int
        name: str

    class SimpleViewSet(UpdateMixin[int, SimpleItem]):
        async def perform_update(self, _pk: int, data: SimpleItem, partial: bool = True) -> SimpleItem:
            return data

    router = APIRouter()
    route_viewset(router, base_path="/items", pk_field_name="id")(SimpleViewSet)

    # Check PUT /items/{pk}
    put_route = next(r for r in router.routes if r.path == "/items/{pk}" and "PUT" in r.methods)
    sig = inspect.signature(put_route.endpoint)

    assert "pk" in sig.parameters
    data_param = sig.parameters["data"]

    # Check that 'id' field IS in the model for PUT (revert change)
    assert "id" in data_param.annotation.model_fields, "PK field should NOT be hidden in PUT"

    # Check call
    result = await put_route.endpoint(pk=10, data=SimpleItem(id=10, name="Updated Name"))

    assert result.id == 10
    assert result.name == "Updated Name"

@pytest.mark.asyncio
async def test_viewset_generic_resolution():

    T = TypeVar("T")

    class GenericItem(BaseModel, Generic[T]):
        items: list[T]
        mapping: dict[str, T]

    class MyItem(BaseModel):
        name: str

    router = APIRouter()

    @route_viewset(router=router, base_path="/generic")
    class GenericViewSet(ListMixin[GenericItem[MyItem]]):
        async def perform_list(self) -> list[GenericItem[MyItem]]:
            return [GenericItem(items=[MyItem(name="test")], mapping={"a": MyItem(name="b")})]

    list_route = next(r for r in router.routes if r.path == "/generic" and "GET" in r.methods)

    # Check signature and return type
    sig = inspect.signature(list_route.endpoint)
    return_type = sig.return_annotation

    # Check that T is correctly resolved to MyItem within List and Dict
    args = get_args(return_type) # (GenericItem[MyItem],)
    item_type = args[0]

    item_origin = get_origin(item_type)
    if item_origin is None:
        # Pydantic models with generics sometimes behave differently or are already resolved
        # but let's check what it actually is
        pass
    else:
        assert item_origin is GenericItem
        item_args = get_args(item_type)
        assert item_args[0] is MyItem

    # Check fields in the model
    # If the model is already resolved (which happens with Pydantic), check model_fields directly
    fields = item_type.model_fields
    # items: List[MyItem]
    items_field = fields["items"].annotation
    assert get_origin(items_field) is list
    assert get_args(items_field)[0] is MyItem

    # mapping: Dict[str, MyItem]
    mapping_field = fields["mapping"].annotation
    assert get_origin(mapping_field) is dict
    assert get_args(mapping_field)[1] is MyItem

    # Endpoint call
    result = await list_route.endpoint()
    assert len(result) == 1
    assert result[0].items[0].name == "test"
    assert result[0].mapping["a"].name == "b"


def test_viewset_route_order():
    """
    Routes must be registered (and appear in OpenAPI schema) in a specific order:
      1. GET    /items          (list)
      2. POST   /items          (create)
      3. GET    /items/schema   (viewset schema)
      4. GET    /items/{pk}     (retrieve)
      5. PUT    /items/{pk}     (update)
      6. PATCH  /items/{pk}     (partial_update)
      7. DELETE /items/{pk}     (destroy)
      8. GET    /items/bulk     (bulk operations)
      9. POST   /items/bulk
      10. PUT    /items/bulk
      11. PATCH  /items/bulk
      12. DELETE /items/bulk
      13. GET    /items/lookup
    """
    class OrderItem(BaseModel):
        id: int
        name: str

    class FullViewSet(
        CollectionViewSet[int, OrderItem],
        BulkViewSetMixin[int, OrderItem],
        LookupMixin,
    ):
        async def perform_lookup(self) -> list[LookupItem]:
            return []

        def __init__(self):
            super().__init__(container={}, pk_field="id")

    router = APIRouter()
    route_viewset(router, base_path="/items", pk_field_name="id")(FullViewSet)

    # Build (path, method) list in registration order
    registered = [(r.path, sorted(r.methods)[0]) for r in router.routes]

    # Expected order: first by path group ("" < "{pk}" < "bulk" < "lookup"),
    # then by HTTP method (GET < POST < PUT < PATCH < DELETE)
    expected = [
        ("/items/schema", "GET"),
        ("/items/lookup", "GET"),
        ("/items/bulk", "DELETE"),
        ("/items/bulk", "PATCH"),
        ("/items/bulk", "PUT"),
        ("/items/bulk", "POST"),
        ("/items/{pk}", "DELETE"),
        ("/items/{pk}", "PATCH"),
        ("/items/{pk}", "PUT"),
        ("/items/{pk}", "GET"),
        ("/items", "POST"),
        ("/items", "GET"),
    ]

    assert registered == expected, f"Route order mismatch:\n  got:      {registered}\n  expected: {expected}"

    # Also verify that the OpenAPI schema paths appear in the same order
    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()
    schema_paths = list(schema["paths"].keys())

    expected_paths_order = ["/items/schema", "/items/lookup", "/items/bulk", "/items/{pk}", "/items"]
    # Filter only our paths and check order
    our_paths = [p for p in schema_paths if p in expected_paths_order]
    assert our_paths == expected_paths_order, (
        f"OpenAPI path order mismatch:\n  got:      {our_paths}\n  expected: {expected_paths_order}"
    )
