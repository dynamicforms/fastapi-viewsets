import inspect

from typing import Any, TypeVar

import pytest

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.mixins import (
    BulkViewSetMixin,
    CreateMixin,
    DestroyMixin,
    ListMixin,
    LookupFilter,
    LookupItem,
    LookupMixin,
    make_all_optional,
    parse_sort_param,
    ReadOnlyViewSetMixin,
    RetrieveMixin,
    SortDirection,
    SortState,
    SortStateColumn,
    UpdateMixin,
    ViewSetMixin,
)


class Item(BaseModel):
    id: int
    name: str
    category: str = "misc"
    score: int = 0


ItemFilter = make_all_optional(Item)

_DB: dict[int, Item] = {
    1: Item(id=1, name="apple", category="fruit", score=5),
    2: Item(id=2, name="banana", category="fruit", score=3),
    3: Item(id=3, name="carrot", category="vegetable", score=4),
    4: Item(id=4, name="cherry", category="fruit", score=5),
}


class ItemViewSet(BulkViewSetMixin[int, Item]):
    async def perform_create(self, data: Item) -> Item:
        return data

    async def perform_bulk_create(self, data: list[Item]) -> list[Item]:
        return data

    async def perform_list(self) -> list[Item]:
        return [Item(id=1, name="test")]

    async def perform_retrieve(self, pk: int) -> Item:
        return Item(id=pk, name="test")

    async def perform_update(self, _pk: int, data: Item, partial: bool = False) -> Item:
        return data

    async def perform_bulk_update(self, records: dict[int, Item], partial: bool = False) -> list[Item]:
        return list(records.values())

    async def perform_destroy(self, pk: int) -> dict[int, Any]:
        return {pk: "deleted"}

    async def perform_bulk_destroy(self, pk: list[int]) -> list[dict[int, Any]]:
        return [{p: "deleted"} for p in pk]

class ReadOnlyItemViewSet(ReadOnlyViewSetMixin[int, Item]):
    async def perform_list(self) -> list[Item]:
        return [Item(id=1, name="test")]

    async def perform_retrieve(self, pk: int) -> Item:
        return Item(id=pk, name="test")

class StandardItemViewSet(ViewSetMixin[int, Item]):
    async def perform_create(self, data: Item) -> Item:
        return data

    async def perform_list(self) -> list[Item]:
        return []

    async def perform_retrieve(self, pk: int) -> Item:
        return Item(id=pk, name="test")

    async def perform_update(self, _pk: int, data: Item, partial: bool = False) -> Item:
        return data

    async def perform_destroy(self, pk: int) -> dict[int, Any]:
        return {pk: "deleted"}

class FilterableItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item, ItemFilter]):
    """ViewSet used in multiple tests; shares the module-level _DB."""

    def __init__(self):
        super().__init__(container=_DB, pk_field="id")

    async def filter_list(self, fltr: Any, records: list[Item]) -> list[Item]:
        results = records
        if fltr.name is not None:
            results = [i for i in results if fltr.name.lower() in i.name.lower()]
        if fltr.category is not None:
            results = [i for i in results if i.category == fltr.category]
        if fltr.score is not None:
            results = [i for i in results if i.score == fltr.score]
        if fltr.id is not None:
            results = [i for i in results if i.id == fltr.id]
        return results


@pytest.mark.asyncio
async def test_viewset_methods():
    viewset = ItemViewSet()
    item = Item(id=1, name="test")

    assert await viewset.create(item) == item
    assert await viewset.bulk_create([item]) == [item]
    assert await viewset.list_items() == [Item(id=1, name="test")]
    assert await viewset.retrieve(1) == Item(id=1, name="test")
    assert await viewset.update(1, item) == item
    assert await viewset.bulk_update({1: item}) == [item]
    assert await viewset.destroy(1) == {1: "deleted"}
    assert await viewset.bulk_destroy([1]) == [{1: "deleted"}]

def test_final_methods():
    # Check if methods are marked as @final (in Python 3.8+ typing.final adds __final__ attribute)
    assert getattr(CreateMixin.create, "__final__", False) or "final" in str(CreateMixin.create)
    # In reality, typing.final in Python adds __final__ = True attribute to the function
    assert CreateMixin.create.__final__ is True
    assert ListMixin.list_items.__final__ is True
    assert RetrieveMixin.retrieve.__final__ is True
    assert UpdateMixin.update.__final__ is True
    assert DestroyMixin.destroy.__final__ is True

"""
Testing abstract declarations
"""
T = TypeVar("T")
K = TypeVar("K")

def test_cannot_instantiate_abstract_mixin():
    # Action mixins no longer declare perform_xxx as abstractmethods — they can be instantiated standalone.
    # The abstract contract lives in ImplMixin; action mixins just provide router methods.
    instance = CreateMixin()
    assert isinstance(instance, CreateMixin)


def test_cannot_instantiate_incomplete_viewset():
    # Without ImplMixin, CreateMixin alone has no abstractmethods — instantiation succeeds,
    # but calling create() would fail at runtime. The abstract contract is enforced via ImplMixin.
    class IncompleteViewSet(CreateMixin[int, str]):
        pass

    instance = IncompleteViewSet()
    assert isinstance(instance, CreateMixin)


def test_can_instantiate_complete_viewset():
    class CompleteViewSet(CreateMixin[int, str]):
        async def perform_create(self, data: str) -> str:
            return data

    # This should work without error
    instance = CompleteViewSet()
    assert isinstance(instance, CreateMixin)


def test_cannot_instantiate_combined_mixins():
    # Combined mixins no longer have abstractmethods — they can be instantiated standalone.
    # The abstract contract is enforced only when combined with ImplMixin.
    assert isinstance(ReadOnlyViewSetMixin(), ReadOnlyViewSetMixin)
    assert isinstance(ViewSetMixin(), ViewSetMixin)
    assert isinstance(BulkViewSetMixin(), BulkViewSetMixin)


# ---------------------------------------------------------------------------
# make_all_optional
# ---------------------------------------------------------------------------

def test_make_optional_filter_model_all_fields_optional():
    for field_name, field_info in ItemFilter.model_fields.items():
        assert field_info.is_required() is False, f"Field '{field_name}' should not be required"
        assert field_info.default is None, f"Field '{field_name}' default should be None"


def test_make_optional_filter_model_field_names_match():
    assert set(ItemFilter.model_fields.keys()) == set(Item.model_fields.keys())


def test_make_optional_filter_model_instantiation():
    instance = ItemFilter()
    assert instance.id is None
    assert instance.name is None
    assert instance.category is None
    assert instance.score is None

    partial = ItemFilter(name="test", score=3)
    assert partial.name == "test"
    assert partial.score == 3
    assert partial.category is None


# ---------------------------------------------------------------------------
# FilterableMixin – route registration
# ---------------------------------------------------------------------------

def test_filterable_mixin_registers_list_route():
    router = APIRouter()
    route_viewset(router, base_path="/items")(FilterableItemViewSet)
    paths_methods = {(r.path, frozenset(r.methods)) for r in router.routes}
    assert ("/items", frozenset({"GET"})) in paths_methods


def test_filterable_mixin_list_has_filter_param():
    router = APIRouter()
    route_viewset(router, base_path="/items")(FilterableItemViewSet)
    list_route = next(r for r in router.routes if r.path == "/items" and "GET" in r.methods)

    sig = inspect.signature(list_route.endpoint)
    assert "fltr" in sig.parameters, "list endpoint should have a 'fltr' parameter"
    assert "self" not in sig.parameters


def test_filterable_mixin_filter_param_uses_optional_model():
    router = APIRouter()
    route_viewset(router, base_path="/items")(FilterableItemViewSet)
    list_route = next(r for r in router.routes if r.path == "/items" and "GET" in r.methods)

    sig = inspect.signature(list_route.endpoint)
    filter_param = sig.parameters["fltr"]
    # The annotation should be an Annotated type wrapping an all-optional model
    ann = filter_param.annotation
    # Unwrap Annotated: __args__[0] is the actual model type
    inner = ann.__args__[0]
    assert issubclass(inner, BaseModel), "filter annotation should wrap a Pydantic model"
    # All fields of that inner model must be optional (have a default of None)
    for fname, finfo in inner.model_fields.items():
        assert finfo.default is None, f"Filter field '{fname}' should default to None"


# ---------------------------------------------------------------------------
# FilterableMixin – runtime behaviour (direct endpoint calls)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_without_filter_returns_all():
    router = APIRouter()
    route_viewset(router, base_path="/items")(FilterableItemViewSet)
    list_route = next(r for r in router.routes if r.path == "/items" and "GET" in r.methods)

    result = await list_route.endpoint(fltr=ItemFilter())
    assert len(result) == len(_DB)


@pytest.mark.asyncio
async def test_list_filter_by_category():
    router = APIRouter()
    route_viewset(router, base_path="/items")(FilterableItemViewSet)
    list_route = next(r for r in router.routes if r.path == "/items" and "GET" in r.methods)

    result = await list_route.endpoint(fltr=ItemFilter(category="fruit"))
    assert all(i.category == "fruit" for i in result)
    assert len(result) == 3  # apple, banana, cherry


@pytest.mark.asyncio
async def test_list_filter_by_name_partial_match():
    router = APIRouter()
    route_viewset(router, base_path="/items")(FilterableItemViewSet)
    list_route = next(r for r in router.routes if r.path == "/items" and "GET" in r.methods)

    result = await list_route.endpoint(fltr=ItemFilter(name="rr"))
    # "carrot" and "cherry" both contain "rr"
    names = {i.name for i in result}
    assert "carrot" in names
    assert "cherry" in names
    assert "apple" not in names
    assert "banana" not in names


@pytest.mark.asyncio
async def test_list_filter_by_score():
    router = APIRouter()
    route_viewset(router, base_path="/items")(FilterableItemViewSet)
    list_route = next(r for r in router.routes if r.path == "/items" and "GET" in r.methods)

    result = await list_route.endpoint(fltr=ItemFilter(score=5))
    assert len(result) == 2  # apple and cherry
    assert all(i.score == 5 for i in result)


@pytest.mark.asyncio
async def test_list_filter_combined():
    router = APIRouter()
    route_viewset(router, base_path="/items")(FilterableItemViewSet)
    list_route = next(r for r in router.routes if r.path == "/items" and "GET" in r.methods)

    result = await list_route.endpoint(fltr=ItemFilter(category="fruit", score=5))
    assert len(result) == 2  # apple and cherry
    assert all(i.category == "fruit" and i.score == 5 for i in result)


@pytest.mark.asyncio
async def test_list_filter_no_match():
    router = APIRouter()
    route_viewset(router, base_path="/items")(FilterableItemViewSet)
    list_route = next(r for r in router.routes if r.path == "/items" and "GET" in r.methods)

    result = await list_route.endpoint(fltr=ItemFilter(name="xyz_does_not_exist"))
    assert result == []


@pytest.mark.asyncio
async def test_list_filter_by_id():
    router = APIRouter()
    route_viewset(router, base_path="/items")(FilterableItemViewSet)
    list_route = next(r for r in router.routes if r.path == "/items" and "GET" in r.methods)

    result = await list_route.endpoint(fltr=ItemFilter(id=2))
    assert len(result) == 1
    assert result[0].name == "banana"


# ---------------------------------------------------------------------------
# FilterableMixin does NOT break other viewset operations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retrieve_still_works_with_filterable_mixin():
    router = APIRouter()
    route_viewset(router, base_path="/items")(FilterableItemViewSet)
    retrieve_route = next(r for r in router.routes if r.path == "/items/{pk}" and "GET" in r.methods)

    result = await retrieve_route.endpoint(pk=3)
    assert result.name == "carrot"


# ---------------------------------------------------------------------------
# OpenAPI schema – filter params appear in Swagger
# ---------------------------------------------------------------------------

def test_filter_params_visible_in_openapi():
    router = APIRouter()
    route_viewset(router, base_path="/items")(FilterableItemViewSet)

    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()

    get_items = schema["paths"]["/items"]["get"]
    param_names = {p["name"] for p in get_items.get("parameters", [])}

    # All Item fields should appear as query parameters
    for field in Item.model_fields:
        assert field in param_names, f"Field '{field}' should appear as a query param in Swagger"


@pytest.mark.asyncio
async def test_list_items_no_filter_fltr_none():
    """list_items with fltr=None (no query params at all) must return all items."""
    router = APIRouter()
    route_viewset(router, base_path="/items")(FilterableItemViewSet)
    list_route = next(r for r in router.routes if r.path == "/items" and "GET" in r.methods)

    result = await list_route.endpoint(fltr=None)
    assert len(result) == len(_DB)
    assert all(isinstance(i, Item) for i in result)


@pytest.mark.asyncio
async def test_list_items_no_filter_via_http():
    """Simulate a Swagger GET /items request without any filter query params."""
    from httpx import ASGITransport, AsyncClient

    router = APIRouter()
    route_viewset(router, base_path="/items")(FilterableItemViewSet)

    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/items")

    assert response.status_code == 200
    assert len(response.json()) == len(_DB)


# ===========================================================================
# LookupMixin – filter support
# ===========================================================================

_LOOKUP_DB: list[LookupItem] = [
    LookupItem(pk=1, title="Apple"),
    LookupItem(pk=2, title="Banana"),
    LookupItem(pk=3, title="Apricot"),
    LookupItem(pk=4, title="Cherry"),
]


class LookupItemViewSet(CollectionViewSet[int, Item], LookupMixin):
    """Uses the default LookupFilter (q only)."""

    def __init__(self):
        super().__init__(container=_DB, pk_field="id")

    async def perform_lookup(self) -> list[LookupItem]:
        return list(_LOOKUP_DB)


# ---------------------------------------------------------------------------
# route registration
# ---------------------------------------------------------------------------

def test_lookup_registers_route():
    router = APIRouter()
    route_viewset(router, base_path="/items")(LookupItemViewSet)
    paths_methods = {(r.path, frozenset(r.methods)) for r in router.routes}
    assert ("/items/lookup", frozenset({"GET"})) in paths_methods


def test_lookup_q_param_visible_in_openapi():
    router = APIRouter()
    route_viewset(router, base_path="/items")(LookupItemViewSet)

    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()

    get_lookup = schema["paths"]["/items/lookup"]["get"]
    param_names = {p["name"] for p in get_lookup.get("parameters", [])}
    assert "q" in param_names, "'q' should appear as a query parameter in Swagger"


# ---------------------------------------------------------------------------
# runtime behaviour – default filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lookup_without_q_returns_all():
    router = APIRouter()
    route_viewset(router, base_path="/items")(LookupItemViewSet)
    lookup_route = next(r for r in router.routes if r.path == "/items/lookup" and "GET" in r.methods)

    result = await lookup_route.endpoint(fltr=LookupFilter())
    assert len(result) == len(_LOOKUP_DB)


@pytest.mark.asyncio
async def test_lookup_with_q_filters_by_title():
    router = APIRouter()
    route_viewset(router, base_path="/items")(LookupItemViewSet)
    lookup_route = next(r for r in router.routes if r.path == "/items/lookup" and "GET" in r.methods)

    result = await lookup_route.endpoint(fltr=LookupFilter(q="ap"))
    titles = {i.title for i in result}
    assert "Apple" in titles
    assert "Apricot" in titles
    assert "Banana" not in titles
    assert "Cherry" not in titles


@pytest.mark.asyncio
async def test_lookup_q_case_insensitive():
    router = APIRouter()
    route_viewset(router, base_path="/items")(LookupItemViewSet)
    lookup_route = next(r for r in router.routes if r.path == "/items/lookup" and "GET" in r.methods)

    result = await lookup_route.endpoint(fltr=LookupFilter(q="APPLE"))
    assert len(result) == 1
    assert result[0].title == "Apple"


@pytest.mark.asyncio
async def test_lookup_q_no_match_returns_empty():
    router = APIRouter()
    route_viewset(router, base_path="/items")(LookupItemViewSet)
    lookup_route = next(r for r in router.routes if r.path == "/items/lookup" and "GET" in r.methods)

    result = await lookup_route.endpoint(fltr=LookupFilter(q="xyz_no_match"))
    assert result == []


@pytest.mark.asyncio
async def test_lookup_fltr_none_returns_all():
    router = APIRouter()
    route_viewset(router, base_path="/items")(LookupItemViewSet)
    lookup_route = next(r for r in router.routes if r.path == "/items/lookup" and "GET" in r.methods)

    result = await lookup_route.endpoint(fltr=None)
    assert len(result) == len(_LOOKUP_DB)


# ---------------------------------------------------------------------------
# setup_lookup_filter pre-event hook
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lookup_setup_filter_hook_called():
    """setup_lookup_filter is called before perform_lookup when filter is active."""
    calls = []

    class HookCheckViewSet(CollectionViewSet[int, Item], LookupMixin):
        def __init__(self):
            super().__init__(container=_DB, pk_field="id")

        async def perform_lookup(self) -> list[LookupItem]:
            return list(_LOOKUP_DB)

        async def setup_lookup_filter(self, fltr: LookupFilter) -> None:
            calls.append(("setup", fltr.q))

    router = APIRouter()
    route_viewset(router, base_path="/items")(HookCheckViewSet)
    lookup_route = next(r for r in router.routes if r.path == "/items/lookup" and "GET" in r.methods)

    await lookup_route.endpoint(fltr=LookupFilter(q="ap"))
    assert calls == [("setup", "ap")]


@pytest.mark.asyncio
async def test_lookup_setup_filter_hook_not_called_without_q():
    """setup_lookup_filter is NOT called when filter is inactive."""
    calls = []

    class HookCheckViewSet(CollectionViewSet[int, Item], LookupMixin):
        def __init__(self):
            super().__init__(container=_DB, pk_field="id")

        async def perform_lookup(self) -> list[LookupItem]:
            return list(_LOOKUP_DB)

        async def setup_lookup_filter(self, fltr: LookupFilter) -> None:
            calls.append("called")

    router = APIRouter()
    route_viewset(router, base_path="/items")(HookCheckViewSet)
    lookup_route = next(r for r in router.routes if r.path == "/items/lookup" and "GET" in r.methods)

    await lookup_route.endpoint(fltr=LookupFilter())
    assert calls == []


# ---------------------------------------------------------------------------
# custom filter model
# ---------------------------------------------------------------------------

class ExtendedLookupFilter(LookupFilter):
    group: str | None = None


class ExtendedLookupViewSet(CollectionViewSet[int, Item], LookupMixin[ExtendedLookupFilter]):
    def __init__(self):
        super().__init__(container=_DB, pk_field="id")

    async def perform_lookup(self) -> list[LookupItem]:
        return list(_LOOKUP_DB)

    async def filter_lookup(self, fltr: ExtendedLookupFilter, items: list[LookupItem]) -> list[LookupItem]:
        if fltr.q is not None:
            items = [i for i in items if fltr.q.lower() in i.title.lower()]
        if fltr.group is not None:
            items = [i for i in items if i.group == fltr.group]
        return items


def test_lookup_custom_filter_params_visible_in_openapi():
    router = APIRouter()
    route_viewset(router, base_path="/items")(ExtendedLookupViewSet)

    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()

    get_lookup = schema["paths"]["/items/lookup"]["get"]
    param_names = {p["name"] for p in get_lookup.get("parameters", [])}
    assert "q" in param_names
    assert "group" in param_names


@pytest.mark.asyncio
async def test_lookup_custom_filter_filters_correctly():
    router = APIRouter()
    route_viewset(router, base_path="/items")(ExtendedLookupViewSet)
    lookup_route = next(r for r in router.routes if r.path == "/items/lookup" and "GET" in r.methods)

    result = await lookup_route.endpoint(fltr=ExtendedLookupFilter(q="a"))
    titles = {i.title for i in result}
    assert "Apple" in titles
    assert "Banana" in titles
    assert "Apricot" in titles
    assert "Cherry" not in titles


# ===========================================================================
# Sort — parse_sort_param
# ===========================================================================

def test_parse_sort_param_single_asc():
    result = parse_sort_param("name:asc")
    assert result == [SortStateColumn(column_name="name", direction=SortDirection.asc)]


def test_parse_sort_param_single_desc():
    result = parse_sort_param("score:desc")
    assert result == [SortStateColumn(column_name="score", direction=SortDirection.desc)]


def test_parse_sort_param_no_direction_defaults_to_asc():
    result = parse_sort_param("name")
    assert result == [SortStateColumn(column_name="name", direction=SortDirection.asc)]


def test_parse_sort_param_multiple():
    result = parse_sort_param("category:asc,score:desc")
    assert result[0].column_name == "category"
    assert result[0].direction == SortDirection.asc
    assert result[1].column_name == "score"
    assert result[1].direction == SortDirection.desc


def test_parse_sort_param_invalid_direction_skipped():
    result = parse_sort_param("name:invalid,score:asc")
    assert len(result) == 1
    assert result[0].column_name == "score"


def test_parse_sort_param_none_returns_empty():
    assert parse_sort_param(None) == []


def test_parse_sort_param_empty_string_returns_empty():
    assert parse_sort_param("") == []


def test_sort_state_column_camel_alias():
    col = SortStateColumn(column_name="myField", direction=SortDirection.desc)
    dumped = col.model_dump(by_alias=True)
    assert dumped == {"columnName": "myField", "direction": "desc"}


# ===========================================================================
# Sort — ListMixin runtime behaviour
# ===========================================================================

class SortableItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item]):
    def __init__(self):
        super().__init__(container=_DB, pk_field="id")


def _get_list_route(viewset_cls):
    router = APIRouter()
    route_viewset(router, base_path="/items")(viewset_cls)
    return next(r for r in router.routes if r.path == "/items" and "GET" in r.methods)


@pytest.mark.asyncio
async def test_sort_param_visible_in_openapi():
    router = APIRouter()
    route_viewset(router, base_path="/items")(SortableItemViewSet)
    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()
    param_names = {p["name"] for p in schema["paths"]["/items"]["get"].get("parameters", [])}
    assert "sort" in param_names


@pytest.mark.asyncio
async def test_sort_no_sort_returns_all():
    route = _get_list_route(SortableItemViewSet)
    result = await route.endpoint(fltr=None, sort=None)
    assert len(result) == len(_DB)


@pytest.mark.asyncio
async def test_sort_by_name_asc():
    route = _get_list_route(SortableItemViewSet)
    result = await route.endpoint(fltr=None, sort="name:asc")
    names = [i.name for i in result]
    assert names == sorted(names)


@pytest.mark.asyncio
async def test_sort_by_name_desc():
    route = _get_list_route(SortableItemViewSet)
    result = await route.endpoint(fltr=None, sort="name:desc")
    names = [i.name for i in result]
    assert names == sorted(names, reverse=True)


@pytest.mark.asyncio
async def test_sort_by_score_then_name():
    route = _get_list_route(SortableItemViewSet)
    # score asc, then name asc as tiebreaker
    result = await route.endpoint(fltr=None, sort="score:asc,name:asc")
    # apple(5), cherry(5), banana(3), carrot(4) → banana(3), carrot(4), apple(5), cherry(5)
    assert result[0].score <= result[1].score <= result[2].score <= result[3].score
    # apple and cherry both score 5: check name order within that group
    score5 = [i.name for i in result if i.score == 5]
    assert score5 == sorted(score5)


@pytest.mark.asyncio
async def test_sort_via_http():
    from httpx import ASGITransport, AsyncClient

    router = APIRouter()
    route_viewset(router, base_path="/items")(SortableItemViewSet)
    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/items?sort=name:asc")

    assert response.status_code == 200
    names = [item["name"] for item in response.json()]
    assert names == sorted(names)


@pytest.mark.asyncio
async def test_sort_setup_hook_called():
    calls = []

    class HookViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item]):
        def __init__(self):
            super().__init__(container=_DB, pk_field="id")

        async def setup_sort(self, sort: SortState) -> None:
            calls.append([col.column_name for col in sort])

    route = _get_list_route(HookViewSet)
    await route.endpoint(fltr=None, sort="name:asc")
    assert calls == [["name"]]


@pytest.mark.asyncio
async def test_sort_setup_hook_not_called_without_sort():
    calls = []

    class HookViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item]):
        def __init__(self):
            super().__init__(container=_DB, pk_field="id")

        async def setup_sort(self, sort: SortState) -> None:
            calls.append("called")

    route = _get_list_route(HookViewSet)
    await route.endpoint(fltr=None, sort=None)
    assert calls == []


@pytest.mark.asyncio
async def test_sort_and_filter_combined():
    """Filter first, then sort the filtered subset."""
    route = _get_list_route(FilterableItemViewSet)
    result = await route.endpoint(fltr=ItemFilter(category="fruit"), sort="name:desc")
    assert all(i.category == "fruit" for i in result)
    names = [i.name for i in result]
    assert names == sorted(names, reverse=True)


@pytest.mark.asyncio
async def test_sort_nulls_last_asc():
    """None values sort after real values for asc direction."""

    class ItemWithOptional(BaseModel):
        id: int
        name: str | None = None

    db = {
        1: ItemWithOptional(id=1, name="banana"),
        2: ItemWithOptional(id=2, name=None),
        3: ItemWithOptional(id=3, name="apple"),
    }

    class NullViewSet(CollectionViewSet[int, ItemWithOptional], BulkViewSetMixin[int, ItemWithOptional]):
        def __init__(self):
            super().__init__(container=db, pk_field="id")

    route = _get_list_route(NullViewSet)
    result = await route.endpoint(fltr=None, sort="name:asc")
    names = [i.name for i in result]
    assert names[-1] is None  # null last


@pytest.mark.asyncio
async def test_sort_nulls_first_desc():
    """None values sort before real values for desc direction."""

    class ItemWithOptional(BaseModel):
        id: int
        name: str | None = None

    db = {
        1: ItemWithOptional(id=1, name="banana"),
        2: ItemWithOptional(id=2, name=None),
        3: ItemWithOptional(id=3, name="apple"),
    }

    class NullViewSet(CollectionViewSet[int, ItemWithOptional], BulkViewSetMixin[int, ItemWithOptional]):
        def __init__(self):
            super().__init__(container=db, pk_field="id")

    route = _get_list_route(NullViewSet)
    result = await route.endpoint(fltr=None, sort="name:desc")
    names = [i.name for i in result]
    assert names[0] is None  # null first
