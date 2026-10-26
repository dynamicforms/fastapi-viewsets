"""
The list pipeline end to end: a lazy perform_list, the apply_* stages, push-down via
mark_applied(), and paginated vs plain listing over a real FastAPI app.
"""

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from .collection_viewset import CollectionViewSet
from .context import Context
from .decorators import route_viewset
from .list_query import ListQuery, ListRecords
from .mixins import ListMixin, make_all_optional, PaginatedListMixin, SortStateColumn


class Track(BaseModel):
    id: int = Field(json_schema_extra={"autoinc_int": True})
    title: str
    year: int


TrackFilter = make_all_optional(Track)


def library(count: int) -> dict[int, Track]:
    return {n: Track(id=n, title=f"Track {n:03d}", year=2000 + (n % 10)) for n in range(1, count + 1)}


def client_for(viewset_cls, base_path: str = "/tracks") -> TestClient:
    app = FastAPI()
    router = APIRouter()
    route_viewset(router, base_path=base_path, pk_field_name="id")(viewset_cls)
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# perform_list may be lazy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_perform_list_may_return_a_generator():
    """
    A viewset that cannot cheaply materialise its rows yields them from perform_list.
    """

    class GeneratorViewSet(ListMixin[Track, None]):
        async def perform_list(self, _context: Context):
            return (Track(id=n, title=f"Track {n}", year=2000) for n in range(5))

    result = await GeneratorViewSet().get_list(None, ListQuery())
    assert [track.id for track in result] == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_perform_list_may_return_an_async_generator():
    class AsyncViewSet(ListMixin[Track, None]):
        async def perform_list(self, _context: Context):
            async def rows():
                for n in range(3):
                    yield Track(id=n, title=f"Track {n}", year=2000)

            return rows()

    result = await AsyncViewSet().get_list(None, ListQuery())
    assert [track.id for track in result] == [0, 1, 2]


@pytest.mark.asyncio
async def test_a_generator_is_only_walked_as_far_as_the_page_needs():
    consumed = 0

    class LazyViewSet(PaginatedListMixin[Track, None]):
        async def perform_list(self, _context: Context):
            def rows():
                nonlocal consumed
                for n in range(10_000):
                    consumed += 1
                    yield Track(id=n, title=f"Track {n}", year=2000)

            return rows()

    page = await LazyViewSet().get_list(None, ListQuery(offset=0, limit=10))
    assert len(page.results) == 10
    assert consumed == 11  # the page, plus one row of lookahead for has_more


# ---------------------------------------------------------------------------
# apply_* stages and push-down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_sort_can_be_overridden_and_chained_with_super():
    calls = []

    class SortingViewSet(ListMixin[Track, None]):
        async def perform_list(self, _context: Context):
            return [Track(id=2, title="b", year=2001), Track(id=1, title="a", year=2000)]

        async def apply_sort(self, context, query: ListQuery, records: ListRecords) -> ListRecords:
            calls.append("override")
            return await super().apply_sort(context, query, records)

    result = await SortingViewSet().get_list(None, ListQuery(sort=[SortStateColumn(column_name="id")]))
    assert calls == ["override"]
    assert [track.id for track in result] == [1, 2]


@pytest.mark.asyncio
async def test_mark_applied_stops_the_default_stage_redoing_the_work():
    """
    A backend that sorted in its own query says so, and the in-memory sort leaves the rows alone
    instead of re-sorting them.
    """

    class PushdownViewSet(ListMixin[Track, None]):
        async def perform_list(self, _context: Context):
            # Deliberately in an order the in-memory sort would change.
            return [Track(id=2, title="b", year=2001), Track(id=1, title="a", year=2000)]

        async def apply_sort(self, context, query: ListQuery, records: ListRecords) -> ListRecords:
            query.mark_applied("sort")
            return await super().apply_sort(context, query, records)

    query = ListQuery(sort=[SortStateColumn(column_name="id")])
    result = await PushdownViewSet().get_list(None, query)
    assert [track.id for track in result] == [2, 1]


@pytest.mark.asyncio
async def test_a_filter_type_with_no_filter_list_returns_records_not_none():
    """
    filter_list has no default body, so it returns None. Passing that on answered the request with
    a null body - visible only to whoever sent a filter to a viewset that never implemented one.
    """

    class UnfilteredViewSet(ListMixin[Track, TrackFilter]):
        async def perform_list(self, _context: Context):
            return [Track(id=1, title="a", year=2000)]

    result = await UnfilteredViewSet().get_list(None, ListQuery(fltr=TrackFilter(id=1)))
    assert result is not None
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


def test_plain_list_mixin_is_unchanged_and_takes_no_paging_parameters():
    class PlainViewSet(CollectionViewSet[int, Track], ListMixin[Track, TrackFilter]):
        def __init__(self):
            super().__init__(container=library(30), pk_field="id")

    client = client_for(PlainViewSet)
    response = client.get("/tracks")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert len(response.json()) == 30

    schema = client.get("/tracks/schema").json()
    parameters = {p["name"] for p in schema["paths"]["/tracks"]["get"].get("parameters", [])}
    assert "limit" not in parameters


def test_paginated_mixin_returns_an_envelope():
    class PagedViewSet(CollectionViewSet[int, Track], PaginatedListMixin[Track, TrackFilter]):
        def __init__(self):
            super().__init__(container=library(30), pk_field="id")

    client = client_for(PagedViewSet)
    body = client.get("/tracks", params={"offset": 0, "limit": 10}).json()
    assert [track["id"] for track in body["results"]] == list(range(1, 11))
    assert body["count"] == 30
    assert body["has_more"] is True
    assert body["has_previous"] is False


def test_the_last_page_reports_no_more_and_a_previous():
    class PagedViewSet(CollectionViewSet[int, Track], PaginatedListMixin[Track, TrackFilter]):
        def __init__(self):
            super().__init__(container=library(25), pk_field="id")

    client = client_for(PagedViewSet)
    body = client.get("/tracks", params={"offset": 20, "limit": 10}).json()
    assert len(body["results"]) == 5
    assert body["has_more"] is False
    assert body["has_previous"] is True


def test_limit_is_capped_at_max_page_size():
    class PagedViewSet(CollectionViewSet[int, Track], PaginatedListMixin[Track, TrackFilter]):
        max_page_size = 5

        def __init__(self):
            super().__init__(container=library(30), pk_field="id")

    client = client_for(PagedViewSet)
    body = client.get("/tracks", params={"limit": 1000}).json()
    assert len(body["results"]) == 5
    assert body["limit"] == 5


def test_omitting_limit_uses_the_viewsets_default_page_size():
    class PagedViewSet(CollectionViewSet[int, Track], PaginatedListMixin[Track, TrackFilter]):
        default_page_size = 7

        def __init__(self):
            super().__init__(container=library(30), pk_field="id")

    client = client_for(PagedViewSet)
    body = client.get("/tracks").json()
    assert len(body["results"]) == 7


def test_paging_composes_with_filtering_and_sorting():
    class PagedViewSet(CollectionViewSet[int, Track], PaginatedListMixin[Track, TrackFilter]):
        def __init__(self):
            super().__init__(container=library(30), pk_field="id")

        async def filter_list(self, fltr, records):
            return [track for track in records if fltr.year is None or track.year == fltr.year]

    client = client_for(PagedViewSet)
    body = client.get("/tracks", params={"year": 2003, "sort": "id:desc", "limit": 2}).json()
    assert all(track["year"] == 2003 for track in body["results"])
    assert [track["id"] for track in body["results"]] == [23, 13]
    assert body["has_more"] is True


def test_the_paginated_response_model_resolves_the_item_type_in_the_schema():
    """
    PaginatedList[T] is a pydantic generic, not a typing alias, so TypeVar resolution has to reach
    into it explicitly - otherwise the schema describes results as a list of anything.
    """

    class PagedViewSet(CollectionViewSet[int, Track], PaginatedListMixin[Track, TrackFilter]):
        def __init__(self):
            super().__init__(container=library(3), pk_field="id")

    client = client_for(PagedViewSet)
    schema = client.get("/tracks/schema").json()
    ref = schema["paths"]["/tracks"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    model = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    assert model["properties"]["results"]["items"]["$ref"].endswith("/Track")


def test_a_filter_value_that_will_not_convert_is_a_422_on_the_query_parameter():
    from .filters import make_filter_model

    class FilteredViewSet(CollectionViewSet[int, Track], ListMixin[Track, make_filter_model(Track, {"year": ["in"]})]):
        def __init__(self):
            super().__init__(container=library(3), pk_field="id")

    client = client_for(FilteredViewSet)
    assert client.get("/tracks", params={"year__in": ["2001", "2002"]}).status_code == 200

    response = client.get("/tracks", params={"year__in": ["2001", "abc"]})
    assert response.status_code == 422
    assert response.json()["detail_code"] == "fastapi-viewsets.validation_failed"
    [error] = response.json()["field_errors"]["year__in.1"]
    assert error["code"] == "pydantic.int_parsing"


def test_in_reads_a_repeated_query_parameter_and_keeps_commas_in_a_value():
    from .filters import make_filter_model

    class Titled(CollectionViewSet[int, Track], ListMixin[Track, make_filter_model(Track, {"title": ["in"]})]):
        def __init__(self):
            super().__init__(
                container={1: Track(id=1, title="a,b", year=2001), 2: Track(id=2, title="a", year=2002)}, pk_field="id"
            )

    client = client_for(Titled)
    assert [r["id"] for r in client.get("/tracks", params={"title__in": ["a,b"]}).json()] == [1]
    assert [r["id"] for r in client.get("/tracks", params={"title__in": ["a,b", "a"]}).json()] == [1, 2]

    [parameter] = [
        p
        for p in client.get("/openapi.json").json()["paths"]["/tracks"]["get"]["parameters"]
        if p["name"] == "title__in"
    ]
    assert parameter["schema"]["anyOf"][0] == {"type": "array", "items": {"type": "string"}}
    assert "repeats" in parameter["description"]


@pytest.mark.parametrize(("parameter", "value"), [("limit", -1), ("limit", 0), ("offset", -1)])
def test_a_page_window_out_of_range_is_a_422(parameter, value):
    class PagedViewSet(CollectionViewSet[int, Track], PaginatedListMixin[Track, None]):
        def __init__(self):
            super().__init__(container=library(3), pk_field="id")

    client = client_for(PagedViewSet)
    response = client.get("/tracks", params={parameter: value})
    assert response.status_code == 422
    assert list(response.json()["field_errors"]) == [parameter]


# ---------------------------------------------------------------------------
# default_sort
# ---------------------------------------------------------------------------


class DefaultSortedViewSet(CollectionViewSet[int, Track], ListMixin[Track, TrackFilter]):
    default_sort = "year:desc,id:asc"

    def __init__(self):
        super().__init__(container=library(12), pk_field="id")


def test_default_sort_orders_a_request_without_sort():
    body = client_for(DefaultSortedViewSet).get("/tracks").json()
    assert [track["id"] for track in body[:4]] == [9, 8, 7, 6]
    assert [track["id"] for track in body if track["year"] == 2000] == [10]
    assert [track["id"] for track in body if track["year"] == 2001] == [1, 11]


def test_a_sent_sort_replaces_default_sort():
    body = client_for(DefaultSortedViewSet).get("/tracks", params={"sort": "id:desc"}).json()
    assert [track["id"] for track in body] == list(range(12, 0, -1))


def test_an_empty_sort_leaves_the_list_unordered():
    body = client_for(DefaultSortedViewSet).get("/tracks", params={"sort": ""}).json()
    assert [track["id"] for track in body] == list(range(1, 13))


def test_default_sort_is_the_sort_parameters_default_in_the_schema():
    schema = client_for(DefaultSortedViewSet).get("/tracks/schema").json()
    (sort,) = [param for param in schema["paths"]["/tracks"]["get"]["parameters"] if param["name"] == "sort"]
    assert sort["schema"]["default"] == "year:desc,id:asc"
    assert sort["description"]


def test_without_default_sort_the_sort_parameter_has_no_default():
    class Unsorted(CollectionViewSet[int, Track], ListMixin[Track, TrackFilter]):
        def __init__(self):
            super().__init__(container=library(3), pk_field="id")

    schema = client_for(Unsorted).get("/tracks/schema").json()
    (sort,) = [param for param in schema["paths"]["/tracks"]["get"]["parameters"] if param["name"] == "sort"]
    assert sort["schema"].get("default") is None


@pytest.mark.asyncio
async def test_default_sort_applies_when_list_items_is_called_directly():
    viewset = DefaultSortedViewSet()
    result = await viewset.list_items(None)
    assert [track.id for track in result[:2]] == [9, 8]


def test_default_sort_feeds_the_cursor_keys():
    from .mixins import CursorListMixin

    class CursorSorted(CollectionViewSet[int, Track], CursorListMixin[Track, TrackFilter]):
        default_sort = "year:desc"

        def __init__(self):
            super().__init__(container=library(12), pk_field="id")

    client = client_for(CursorSorted)
    first = client.get("/tracks", params={"limit": 3}).json()
    second = client.get("/tracks", params={"limit": 3, "cursor": first["next"]}).json()
    assert [track["id"] for track in first["results"] + second["results"]] == [9, 8, 7, 6, 5, 4]


@pytest.mark.parametrize(
    ("declared", "message"),
    [
        ("title:up", "is not 'asc' or 'desc'"),
        ("title,,id", "empty entry"),
        ("rating:desc", "has no field 'rating'"),
    ],
)
def test_an_invalid_default_sort_is_refused_when_the_viewset_is_routed(declared, message):
    class Invalid(CollectionViewSet[int, Track], ListMixin[Track, TrackFilter]):
        default_sort = declared

        def __init__(self):
            super().__init__(container=library(3), pk_field="id")

    with pytest.raises(ValueError, match=message):
        client_for(Invalid)


def test_default_sort_may_name_a_computed_field():
    from pydantic import computed_field

    from .mixins import check_declared_sort

    class Named(BaseModel):
        first: str
        last: str

        @computed_field
        @property
        def full_name(self) -> str:
            return f"{self.first} {self.last}"

    assert check_declared_sort("full_name:desc", Named)[0].column_name == "full_name"
