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

from ..cursor import CursorPredicate, decode_cursor
from ..filters import Filter, make_filter_model, register_operator
from ..list_query import build_list_query, ListQuery
from ..mixins import CursorListMixin, make_all_optional, PaginatedListMixin, SortStateColumn
from .django_orm import _cursor_condition, DjangoORMViewSet


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


TrackFilter = make_filter_model(
    Track,
    {
        "title": ["exact", "icontains"],
        "artist": ["exact"],
        "year": ["exact", "gte", "lte", "in"],
    },
)


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
        TrackModel(title=f"Track {n:03d}", artist=f"Artist {n % 5}", year=2000 + (n % 5)) for n in range(1, 31)
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
async def test_a_descending_sort_is_pushed_into_sql_too(viewset):
    """
    `F(col).desc(nulls_first=True)` states the NULL placement outright, which is what `nulls`
    means, so both directions reach SQL and agree with the in-memory comparison.
    """
    query = ListQuery(sort=[SortStateColumn(column_name="year", direction="desc")], limit=5)
    page = await viewset.get_list(None, query)
    assert "sort" in query.applied
    assert [track.year for track in page.results] == sorted((track.year for track in page.results), reverse=True)


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
    TagModel.objects.bulk_create(
        [
            TagModel(title="one", tags="jazz,bop"),
            TagModel(title="two", tags="rock"),
        ]
    )
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


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------


class CursorTrackViewSet(DjangoORMViewSet[int, Track], CursorListMixin[Track, TrackFilter]):
    model = TrackModel
    schema = Track
    default_page_size = 4


async def _walk(viewset, sort):
    """
    Follows `next` to the end, returning every page's titles.

    Async rather than wrapping asyncio.run(): that closes the loop it created and leaves the thread
    with none, which breaks every later test that expects one.
    """
    pages, cursor = [], None
    while True:
        query = build_list_query(None, sort, limit=4)
        query.cursor = cursor
        page = await viewset.get_list(None, query)
        pages.append([record.title for record in page.results])
        cursor = page.next
        if not cursor:
            return pages


def test_a_uniform_ascending_cursor_uses_a_row_value_comparison():
    """
    The fast path: one predicate a composite index can seek with, rather than a union the planner
    turns into a scan.
    """
    keys = (("year", False), ("id", False))
    predicate = CursorPredicate(
        field="", value={}, keys=keys, position={"year": 2003, "id": 5}, backwards=False, inclusive=False
    )
    sql = str(TrackModel.objects.filter(_cursor_condition(CursorTrackViewSet(), predicate)).query)
    assert '("backends_trackmodel"."year", "backends_trackmodel"."id") > (2003, 5)' in sql


def test_mixed_directions_fall_back_to_the_segment_union():
    """A row constructor cannot say `a ASC, b DESC`, so this one has to be spelled out."""
    keys = (("year", True), ("id", False))
    predicate = CursorPredicate(
        field="", value={}, keys=keys, position={"year": 2003, "id": 5}, backwards=False, inclusive=False
    )
    sql = str(TrackModel.objects.filter(_cursor_condition(CursorTrackViewSet(), predicate)).query)
    assert ") > (" not in sql
    assert "OR" in sql


def test_an_inclusive_cursor_falls_back_and_keeps_its_own_anchor():
    keys = (("year", False), ("id", False))
    predicate = CursorPredicate(
        field="", value={}, keys=keys, position={"year": 2003, "id": 5}, backwards=False, inclusive=True
    )
    condition = _cursor_condition(CursorTrackViewSet(), predicate)
    sql = str(TrackModel.objects.filter(condition).query)
    assert ") > (" not in sql  # the anchor row has to be OR-ed back in, so no row-value fast path


@pytest.mark.asyncio
async def test_the_cursor_predicate_reaches_sql(viewset):  # noqa: ARG001
    query = build_list_query(None, "id:asc", limit=4)
    page = await CursorTrackViewSet().get_list(None, query)
    assert [record.title for record in page.results] == ["Track 001", "Track 002", "Track 003", "Track 004"]

    second = build_list_query(None, "id:asc", limit=4)
    second.cursor = page.next
    page_two = await CursorTrackViewSet().get_list(None, second)
    assert "filter" in second.applied  # translated, not filtered in memory
    assert [record.title for record in page_two.results] == [
        "Track 005",
        "Track 006",
        "Track 007",
        "Track 008",
    ]


@pytest.mark.asyncio
async def test_walking_a_table_by_cursor_visits_every_row_once():
    pages = await _walk(CursorTrackViewSet(), "id:asc")
    titles = [title for page in pages for title in page]
    assert titles == [f"Track {n:03d}" for n in range(1, 31)]


@pytest.mark.asyncio
async def test_walking_a_multi_key_ordering_with_ties_visits_every_row_once():
    """`year` takes five values across thirty rows, so every page boundary lands inside a tie."""
    pages = await _walk(CursorTrackViewSet(), "year:asc")
    titles = [title for page in pages for title in page]
    assert sorted(titles) == [f"Track {n:03d}" for n in range(1, 31)]
    assert len(titles) == len(set(titles))


# ---------------------------------------------------------------------------
# A primary key that is not called `id`
# ---------------------------------------------------------------------------
# The cursor appends the primary key to its ordering and filters on it, and this backend pushes a
# filter set down only when every name in it is a concrete model field. The viewsets below declare
# no `pk_field_name`; the backend takes it from the model.


class CodedTrackModel(models.Model):
    code = models.CharField(max_length=20, primary_key=True)
    title = models.CharField(max_length=200)
    year = models.IntegerField()

    class Meta:
        app_label = "backends"


class CodedTrack(BaseModel):
    code: str
    title: str
    year: int


CodedTrackFilter = make_filter_model(CodedTrack, {"title": ["icontains"], "year": ["gte"]})


class CodedTrackViewSet(DjangoORMViewSet[str, CodedTrack], CursorListMixin[CodedTrack, CodedTrackFilter]):
    model = CodedTrackModel
    schema = CodedTrack
    default_page_size = 4


@pytest.fixture
def coded_rows():
    """Sync, because Django refuses schema and ORM calls from inside a running event loop."""
    with connection.schema_editor() as editor:
        editor.create_model(CodedTrackModel)
    CodedTrackModel.objects.bulk_create(
        [CodedTrackModel(code=f"T{n:03d}", title=f"Track {n:03d}", year=2000 + (n % 5)) for n in range(1, 31)]
    )
    yield
    with connection.schema_editor() as editor:
        editor.delete_model(CodedTrackModel)


def test_the_primary_key_name_comes_from_the_model():
    assert CodedTrackViewSet().pk_field_name == "code"


def test_an_explicit_primary_key_name_still_wins():
    """A response schema that renames the primary key is the case the model cannot answer."""

    class OverriddenViewSet(CodedTrackViewSet):
        pk_field_name = "title"

    assert OverriddenViewSet().pk_field_name == "title"


def test_an_abstract_viewset_with_no_model_yet_keeps_the_default():
    """A base that leaves the model to its subclasses has to survive being defined and read."""

    class AbstractCodedViewSet(DjangoORMViewSet[str, CodedTrack], CursorListMixin[CodedTrack, CodedTrackFilter]):
        schema = CodedTrack

    assert AbstractCodedViewSet().pk_field_name == "id"


@pytest.mark.asyncio
async def test_a_renamed_primary_key_joins_the_ordering_and_reaches_sql(coded_rows):  # noqa: ARG001
    """The derived primary key joins the ordering on page one, where no cursor exists yet, and the
    sort and the page still reach SQL."""
    query = build_list_query(None, "title:asc", limit=4)
    page = await CodedTrackViewSet().get_list(None, query)
    assert query._cursor_keys == (("title", False), ("code", False))
    assert query.applied == {"sort", "pagination"}
    assert [record.title for record in page.results] == [
        "Track 001",
        "Track 002",
        "Track 003",
        "Track 004",
    ]


@pytest.mark.asyncio
async def test_the_cursor_carries_the_real_primary_key_value(coded_rows):  # noqa: ARG001
    viewset = CodedTrackViewSet()
    query = build_list_query(None, "title:asc", limit=4)
    page = await viewset.get_list(None, query)

    state = decode_cursor(page.next, query._cursor_keys, query._cursor_query, viewset._cursor_annotations())
    assert state.position == {"title": "Track 004", "code": "T004"}


@pytest.mark.asyncio
async def test_walking_a_renamed_primary_key_visits_every_row_once(coded_rows):  # noqa: ARG001
    """
    `year:asc`, not `title:asc`: a unique first key settles every comparison before the appended
    primary key is read, so a wrong primary key name would still walk the whole table. A tie-heavy
    key reaches the appended key, and a key the ordering cannot compare drops every row tied with
    the anchor.
    """
    pages = await _walk(CodedTrackViewSet(), "year:asc")
    titles = [title for page in pages for title in page]
    assert sorted(titles) == [f"Track {n:03d}" for n in range(1, 31)]
    assert len(titles) == len(set(titles))


@pytest.mark.asyncio
async def test_a_client_filter_survives_the_cursor_on_a_renamed_primary_key(coded_rows):  # noqa: ARG001
    """
    Push-down is all or nothing, and the cursor's predicate shares the filter set with the client's
    own filters: an ordering key the model does not have would take `year__gte` out of SQL with it
    from the second page on. The key comes from the model, so the whole set stays in SQL and page
    two is filtered, sorted and limited by the database.
    """
    viewset = CodedTrackViewSet()
    first = build_list_query(CodedTrackFilter(year__gte=2002), "title:asc", limit=4)
    page = await viewset.get_list(None, first)

    second = build_list_query(CodedTrackFilter(year__gte=2002), "title:asc", limit=4)
    second.cursor = page.next
    page_two = await viewset.get_list(None, second)

    assert second.applied == {"filter", "sort", "pagination"}
    assert all(record.year >= 2002 for record in page_two.results)
    assert [record.title for record in page_two.results] == [
        "Track 008",
        "Track 009",
        "Track 012",
        "Track 013",
    ]


@pytest.mark.asyncio
async def test_a_renamed_primary_key_retrieves_updates_and_destroys(coded_rows):  # noqa: ARG001
    """
    `pk_field` answers a different question and needs no derivation: `pk` is Django's alias for
    whichever field holds the key, so a lookup by key works whatever it is called. The destroy
    response names it `id` regardless of the field.
    """
    viewset = CodedTrackViewSet()
    assert (await viewset.perform_retrieve(None, "T005")).title == "Track 005"

    updated = await viewset.perform_update(None, "T005", CodedTrack(code="T005", title="Renamed", year=1999))
    assert updated.title == "Renamed"

    assert await viewset.perform_destroy(None, "T005") == {"id": "T005"}


# ---------------------------------------------------------------------------
# Primary keys the model and the response schema call different things
# ---------------------------------------------------------------------------
# `_meta.pk` is a relation on both of these: the parent link of an inherited model, and a
# one-to-one field declared as the key. The model knows it as `artist`, the response schema
# carries `artist_id`, and the cursor reads its ordering out of the model and its position out of
# the schema - so only a name both of them have walks the table.


class MediaModel(models.Model):
    title = models.CharField(max_length=200)

    class Meta:
        app_label = "backends"


class SongModel(MediaModel):
    year = models.IntegerField()

    class Meta:
        app_label = "backends"


class Song(BaseModel):
    id: int
    title: str
    year: int


SongFilter = make_filter_model(Song, {"title": ["icontains"], "year": ["gte"]})


class SongViewSet(DjangoORMViewSet[int, Song], CursorListMixin[Song, SongFilter]):
    model = SongModel
    schema = Song
    default_page_size = 4


class ArtistModel(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        app_label = "backends"


class ArtistProfileModel(models.Model):
    artist = models.OneToOneField(ArtistModel, primary_key=True, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    year = models.IntegerField()

    class Meta:
        app_label = "backends"


class ArtistProfile(BaseModel):
    artist_id: int
    title: str
    year: int


ArtistProfileFilter = make_filter_model(ArtistProfile, {"title": ["icontains"], "year": ["gte"]})


class ArtistProfileViewSet(DjangoORMViewSet[int, ArtistProfile], CursorListMixin[ArtistProfile, ArtistProfileFilter]):
    model = ArtistProfileModel
    schema = ArtistProfile
    default_page_size = 4


@pytest.fixture
def song_rows():
    """Nine rows on one year, so the sort key ties on every pair and the appended key decides."""
    with connection.schema_editor() as editor:
        editor.create_model(MediaModel)
        editor.create_model(SongModel)
    for n in range(1, 10):
        SongModel.objects.create(title=f"Song {n:03d}", year=2000)
    yield
    with connection.schema_editor() as editor:
        editor.delete_model(SongModel)
        editor.delete_model(MediaModel)


@pytest.fixture
def artist_profile_rows():
    """Nine rows on one year, so the sort key ties on every pair and the appended key decides."""
    with connection.schema_editor() as editor:
        editor.create_model(ArtistModel)
        editor.create_model(ArtistProfileModel)
    for n in range(1, 10):
        artist = ArtistModel.objects.create(name=f"Artist {n:03d}")
        ArtistProfileModel.objects.create(artist=artist, title=f"Profile {n:03d}", year=2000)
    yield
    with connection.schema_editor() as editor:
        editor.delete_model(ArtistProfileModel)
        editor.delete_model(ArtistModel)


def test_an_inherited_model_keys_on_the_name_its_schema_carries():
    """
    `SongModel._meta.pk` is the parent link, `mediamodel_ptr`. Django reports it as concrete, so it
    would push down and then read a null position off a response model that has no such field.
    """
    assert SongModel._meta.pk.name == "mediamodel_ptr"
    assert SongViewSet().pk_field_name == "id"


@pytest.mark.asyncio
async def test_walking_an_inherited_model_on_a_tied_key_visits_every_row_once(song_rows):  # noqa: ARG001
    pages = await _walk(SongViewSet(), "year:asc")
    titles = [title for page in pages for title in page]
    assert sorted(titles) == [f"Song {n:03d}" for n in range(1, 10)]
    assert len(titles) == len(set(titles))


def test_a_relation_primary_key_keys_on_the_column_its_schema_carries():
    """A one-to-one primary key is `artist` on the model and `artist_id` on the response model."""
    assert ArtistProfileModel._meta.pk.name == "artist"
    assert ArtistProfileViewSet().pk_field_name == "artist_id"


@pytest.mark.asyncio
async def test_walking_a_relation_primary_key_on_a_tied_key_visits_every_row_once(
    artist_profile_rows,  # noqa: ARG001
):
    pages = await _walk(ArtistProfileViewSet(), "year:asc")
    titles = [title for page in pages for title in page]
    assert sorted(titles) == [f"Profile {n:03d}" for n in range(1, 10)]
    assert len(titles) == len(set(titles))


@pytest.mark.asyncio
async def test_a_relation_primary_key_still_pushes_the_cursor_down(artist_profile_rows):  # noqa: ARG001
    """
    `artist_id` is the attribute name of a field Django calls `artist`, and both name the same
    column in a lookup - so the cursor's predicate on it goes into SQL rather than taking the whole
    filter set to the in-memory pass.
    """
    viewset = ArtistProfileViewSet()
    first = build_list_query(None, "year:asc", limit=4)
    page = await viewset.get_list(None, first)

    second = build_list_query(None, "year:asc", limit=4)
    second.cursor = page.next
    await viewset.get_list(None, second)

    assert second._cursor_keys == (("year", False), ("artist_id", False))
    assert second.applied == {"filter", "sort", "pagination"}


def test_a_composite_primary_key_says_so():
    """No single `pk_field_name` names a composite key, and a silent fallback would page wrongly."""
    composite = pytest.importorskip("django.db.models.fields.composite")

    class CompositeKeyModel(models.Model):
        pk = composite.CompositePrimaryKey("label", "revision")
        label = models.CharField(max_length=20)
        revision = models.IntegerField()

        class Meta:
            app_label = "backends"

    class CompositeKey(BaseModel):
        label: str
        revision: int

    class CompositeKeyViewSet(
        DjangoORMViewSet[str, CompositeKey],
        CursorListMixin[CompositeKey, make_filter_model(CompositeKey, {"label": ["exact"]})],
    ):
        model = CompositeKeyModel
        schema = CompositeKey

    with pytest.raises(TypeError, match="composite primary key"):
        _ = CompositeKeyViewSet().pk_field_name
