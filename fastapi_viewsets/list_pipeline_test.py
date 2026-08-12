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
# perform_list may now be lazy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_perform_list_may_return_a_generator():
    """
    The point of the rework: a viewset that cannot cheaply materialise its rows no longer has to.
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
    This is what replaces the setup_sort side channel: a backend that sorted in its own query says
    so, and the in-memory sort leaves the rows alone instead of re-sorting them.
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


@pytest.mark.asyncio
async def test_the_legacy_setup_hooks_still_run_but_warn():
    seen = []

    class LegacyViewSet(ListMixin[Track, TrackFilter]):
        async def perform_list(self, _context: Context):
            return [Track(id=1, title="a", year=2000)]

        async def setup_filter(self, fltr) -> None:
            seen.append(fltr)

        async def filter_list(self, fltr, records):  # noqa: ARG002 - hook signature
            return records

    with pytest.warns(DeprecationWarning, match="setup_filter"):
        await LegacyViewSet().get_list(None, ListQuery(fltr=TrackFilter(id=1)))
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_viewset_without_the_legacy_hooks_does_not_warn():
    import warnings

    class ModernViewSet(ListMixin[Track, TrackFilter]):
        async def perform_list(self, _context: Context):
            return [Track(id=1, title="a", year=2000)]

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        await ModernViewSet().get_list(None, ListQuery(fltr=TrackFilter(id=1)))


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
