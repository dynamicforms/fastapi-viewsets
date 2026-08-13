"""
The Django ORM backend, against a real SQLite database.

The point of these tests is not that Django works - it does - but that the pipeline's push-down
contract survives contact with a backend that actually has a query language: that a stage the
backend absorbed is not redone in memory, that a stage it declined still produces the right answer,
and that paging a queryset reads a page rather than a table.
"""

import pytest


# Django itself is configured once for the whole session in the repo-root conftest.py - see the
# note there on why it cannot be done per module.
from django.db import connection, models
from pydantic import BaseModel

from ..list_query import ListQuery
from ..mixins import make_all_optional, PaginatedListMixin, SortStateColumn
from .django_orm import DjangoORMViewSet


class TrackModel(models.Model):
    title = models.CharField(max_length=200)
    artist = models.CharField(max_length=200)
    year = models.IntegerField()

    class Meta:
        app_label = "backends"


class Track(BaseModel):
    id: int
    title: str
    artist: str
    year: int


TrackFilter = make_all_optional(Track)


class TrackViewSet(DjangoORMViewSet[int, Track], PaginatedListMixin[Track, TrackFilter]):
    model = TrackModel
    schema = Track
    default_page_size = 10


@pytest.fixture(scope="module", autouse=True)
def _database():
    with connection.schema_editor() as editor:
        editor.create_model(TrackModel)
    TrackModel.objects.bulk_create(
        TrackModel(title=f"Track {n:03d}", artist=f"Artist {n % 5}", year=2000 + (n % 5))
        for n in range(1, 31)
    )
    yield
    with connection.schema_editor() as editor:
        editor.delete_model(TrackModel)


@pytest.fixture
def viewset() -> TrackViewSet:
    return TrackViewSet()


# ---------------------------------------------------------------------------
# Laziness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_perform_list_returns_an_unevaluated_queryset(viewset):
    """If this ever materialises, every push-down below it has nothing left to push into."""
    from django.db.models import QuerySet

    records = await viewset.perform_list(None)
    assert isinstance(records, QuerySet)
    assert records._result_cache is None


@pytest.mark.asyncio
async def test_paging_reads_a_page_not_a_table(viewset):
    """
    Asserted against the compiled SQL rather than against captured queries: the async ORM calls run
    on a worker thread, and Django's connections - queries_log included - are per-thread, so
    nothing they log is visible from here.
    """
    query = ListQuery(offset=5, limit=3)
    page = await viewset.get_list(None, query)
    assert [track.title for track in page.results] == ["Track 006", "Track 007", "Track 008"]
    assert "pagination" in query.applied

    sql = str((await viewset.perform_list(None))[5:9].query).upper()
    assert "LIMIT" in sql


@pytest.mark.asyncio
async def test_count_is_a_real_total_not_a_null(viewset):
    """A lazy source would report None; a queryset can answer with one extra COUNT(*)."""
    page = await viewset.get_list(None, ListQuery(offset=0, limit=3))
    assert page.count == 30
    assert page.has_more is True


# ---------------------------------------------------------------------------
# Push-down
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_exact_filter_is_pushed_into_sql(viewset):
    query = ListQuery(fltr=TrackFilter(year=2003), limit=50)
    page = await viewset.get_list(None, query)
    assert "filter" in query.applied
    assert page.results
    assert all(track.year == 2003 for track in page.results)


@pytest.mark.asyncio
async def test_an_ascending_sort_is_pushed_into_sql(viewset):
    query = ListQuery(sort=[SortStateColumn(column_name="year")], limit=5)
    page = await viewset.get_list(None, query)
    assert "sort" in query.applied
    assert [track.year for track in page.results] == sorted(track.year for track in page.results)


@pytest.mark.asyncio
async def test_a_descending_sort_falls_back_and_still_sorts(viewset):
    """
    Declined on purpose: this library puts NULL lowest in both directions, SQL's DESC does not.
    The answer must still be right - the fallback is the design working, not a gap in it.
    """
    query = ListQuery(sort=[SortStateColumn(column_name="year", direction="desc")], limit=5)
    page = await viewset.get_list(None, query)
    assert "sort" not in query.applied
    assert [track.year for track in page.results] == sorted(
        (track.year for track in page.results), reverse=True
    )


@pytest.mark.asyncio
async def test_a_filter_that_cannot_be_pushed_is_not_marked_applied():
    """
    Marking the stage after pushing only half of it would silently return too many rows, so a field
    the backend could not translate has to leave the whole stage to the in-memory pass.
    """
    class UntranslatableViewSet(TrackViewSet):
        async def filter_list(self, fltr, records):
            return [track for track in records if fltr.title is None or fltr.title in track.title]

        def build_filter_criteria(self, fltr):
            criteria, _ = super().build_filter_criteria(fltr)
            return criteria, False

    query = ListQuery(fltr=TrackFilter(title="Track 007"), limit=50)
    page = await UntranslatableViewSet().get_list(None, query)
    assert "filter" not in query.applied
    assert [track.title for track in page.results] == ["Track 007"]


@pytest.mark.asyncio
async def test_filter_and_sort_compose_in_one_query(viewset):
    query = ListQuery(
        fltr=TrackFilter(artist="Artist 3"),
        sort=[SortStateColumn(column_name="year")],
        limit=3,
    )
    page = await viewset.get_list(None, query)
    assert query.applied == {"filter", "sort", "pagination"}
    assert all(track.artist == "Artist 3" for track in page.results)


# ---------------------------------------------------------------------------
# Conversion and single-record operations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rows_are_converted_to_the_response_model(viewset):
    page = await viewset.get_list(None, ListQuery(limit=1))
    assert isinstance(page.results[0], Track)


@pytest.mark.asyncio
async def test_retrieve_create_update_and_destroy(viewset):
    created = await viewset.perform_create(None, Track(id=0, title="New", artist="A", year=1999))
    assert created.title == "New"
    assert created.id  # assigned by the database, not taken from the request body

    fetched = await viewset.perform_retrieve(None, created.id)
    assert fetched.title == "New"

    renamed = Track(id=created.id, title="Renamed", artist="A", year=1999)
    updated = await viewset.perform_update(None, created.id, renamed)
    assert updated.title == "Renamed"

    assert await viewset.perform_destroy(None, created.id) == {"id": created.id}


@pytest.mark.asyncio
async def test_a_missing_record_is_a_404_not_a_crash(viewset):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        await viewset.perform_retrieve(None, 999999)
    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_the_primary_key_in_the_body_cannot_overwrite_another_row(viewset):
    """`id` is not writable: the database assigns it, and honouring it would let a create land on
    top of an existing row."""
    created = await viewset.perform_create(None, Track(id=1, title="Impostor", artist="A", year=1999))
    assert created.id != 1
    assert (await viewset.perform_retrieve(None, 1)).title == "Track 001"
    await viewset.perform_destroy(None, created.id)
