"""
The Django ORM backend, against a real SQLite database.

The point of these tests is not that Django works - it does - but that the pipeline's push-down
contract survives contact with a backend that actually has a query language: that a stage the
backend absorbed is not redone in memory, that a stage it declined still produces the right answer,
and that paging a queryset reads a page rather than a table.
"""

from dataclasses import dataclass
from typing import ClassVar

import pytest


# Django itself is configured once for the whole session in the repo-root conftest.py - see the
# note there on why it cannot be done per module.
from django.db import connection, models
from pydantic import BaseModel

from ..filters import Filter, make_filter_model, register_operator
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


TrackFilter = make_filter_model(Track, {
    "title": ["exact", "icontains"],
    "artist": ["exact"],
    "year": ["exact", "gte", "lte", "in"],
})

class _Tagged(BaseModel):
    id: int
    title: str
    tags: list[str]


TaggedFilter = make_filter_model(_Tagged, {"tags": ["overlaps"], "title": ["exact"]})

LegacyTrackFilter = make_all_optional(Track)
"""A hand-made filter model, with no declaration for the backend to translate."""


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


@dataclass(frozen=True)
class EndsWith(Filter):
    """
    A filter defined outside the library, with no compiler registered for any backend - which is
    all it takes to add an operator: one `matches()`, and it works everywhere immediately.
    """

    lookup: ClassVar[str] = "endswith_demo"

    def matches(self, record) -> bool:
        actual = self.read(record)
        return actual is not None and str(actual).endswith(str(self.value))


register_operator(EndsWith)


@pytest.mark.asyncio
async def test_an_operator_with_no_compiler_leaves_the_whole_stage_alone(viewset):  # noqa: ARG001
    """
    All or nothing: pushing the filters it understands and forgetting the rest would return too
    many rows while looking like it worked. One untranslatable operator sends the whole set to the
    in-memory pass - which still gives the right answer, because every filter can always do that.
    """
    extended = make_filter_model(Track, {"title": ["icontains", "endswith_demo"]})
    query = ListQuery(fltr=extended(title__icontains="track", title__endswith_demo="007"), limit=50)
    page = await viewset.get_list(None, query)
    assert "filter" not in query.applied
    assert [track.title for track in page.results] == ["Track 007"]


@pytest.mark.asyncio
async def test_a_declared_operator_is_pushed_down_once_every_filter_has_a_compiler(viewset):
    """The same declaration minus the uncompilable operator does reach SQL."""
    query = ListQuery(fltr=TrackFilter(title__icontains="track 007"), limit=50)
    page = await viewset.get_list(None, query)
    assert "filter" in query.applied
    assert [track.title for track in page.results] == ["Track 007"]


@pytest.mark.asyncio
async def test_a_hand_made_filter_model_still_uses_filter_list():
    """No declaration means nothing declarative to translate, and the older path takes it."""
    class LegacyViewSet(TrackViewSet):
        async def filter_list(self, fltr, records):
            return [track for track in records if fltr.title is None or fltr.title == track.title]

    query = ListQuery(fltr=LegacyTrackFilter(title="Track 007"), limit=50)
    page = await LegacyViewSet().get_list(None, query)
    assert "filter" not in query.applied
    assert [track.title for track in page.results] == ["Track 007"]


@pytest.mark.asyncio
async def test_a_range_of_operators_compiles_into_one_query(viewset):
    query = ListQuery(fltr=TrackFilter(year__gte=2002, year__lte=2003, title__icontains="0"), limit=50)
    page = await viewset.get_list(None, query)
    assert "filter" in query.applied
    assert page.results
    assert all(2002 <= track.year <= 2003 for track in page.results)


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


class TagModel(models.Model):
    title = models.CharField(max_length=200)
    tags = models.TextField()

    class Meta:
        app_label = "backends"


class TaggedViewSet(DjangoORMViewSet[int, _Tagged], PaginatedListMixin[_Tagged, TaggedFilter]):
    model = TagModel
    schema = _Tagged

    def to_record(self, raw):
        if isinstance(raw, _Tagged):
            return raw
        return _Tagged(id=raw.id, title=raw.title, tags=raw.tags.split(",") if raw.tags else [])


@pytest.fixture
def tagged_rows():
    """Sync, because Django refuses schema and ORM calls from inside a running event loop."""
    with connection.schema_editor() as editor:
        editor.create_model(TagModel)
    TagModel.objects.bulk_create([
        TagModel(title="one", tags="jazz,bop"),
        TagModel(title="two", tags="rock"),
    ])
    yield
    with connection.schema_editor() as editor:
        editor.delete_model(TagModel)


@pytest.mark.asyncio
async def test_the_in_memory_fallback_sees_response_models_not_raw_rows(tagged_rows):  # noqa: ARG001
    """
    Regression: a filter declaration names the *response* model's fields, but the source yields
    whatever the backend stores. This viewset keeps a list as a comma-joined string, and before
    land() converted first, `overlaps` compared a set of characters against a set of words and
    matched nothing at all while looking like it had worked.
    """
    query = ListQuery(fltr=TaggedFilter(tags__overlaps="jazz"), limit=10)
    page = await TaggedViewSet().get_list(None, query)
    assert "filter" not in query.applied  # overlaps has no compiler, so this is the fallback
    assert [record.title for record in page.results] == ["one"]
    assert page.results[0].tags == ["jazz", "bop"]
