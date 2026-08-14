"""
Cursor pagination in memory: the encoding, the predicate, and walking a list end to end.

The properties worth pinning are the ones offset paging gets wrong - that a page never repeats or
skips a row when the collection changes underneath it - and the ones a single-key cursor gets
wrong, which is everything to do with multi-key ordering and ties.
"""

import types

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from .collection_viewset import CollectionViewSet
from .cursor import (
    cursor_keys,
    CursorError,
    CursorPredicate,
    CursorState,
    decode_cursor,
    fingerprint,
    position_of,
)
from .decorators import route_viewset
from .filters import make_filter_model
from .mixins import CursorListMixin, SortDirection, SortStateColumn


class Track(BaseModel):
    id: int
    title: str
    year: int | None


TrackFilter = make_filter_model(Track, {"year": ["exact", "gte"], "title": ["icontains"]})

KEYS = (("year", False), ("id", False))


def track(id_: int, year: int | None = 2000, title: str = "x") -> Track:
    return Track(id=id_, title=title, year=year)


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------

def test_the_primary_key_is_appended_to_the_ordering():
    """Without it two rows can share a position, and then a page boundary has no defined place."""
    keys = cursor_keys([SortStateColumn(column_name="year")], "id")
    assert keys == (("year", False), ("id", False))


def test_the_primary_key_is_not_appended_twice():
    keys = cursor_keys([SortStateColumn(column_name="id", direction=SortDirection.desc)], "id")
    assert keys == (("id", True),)


def test_the_real_primary_key_name_is_used():
    assert cursor_keys([], "uuid") == (("uuid", False),)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def test_a_cursor_round_trips():
    state = CursorState({"year": 2003, "id": 42}, backwards=True, inclusive=True, query="abc")
    decoded = decode_cursor(state.encode(), KEYS, "abc", {"year": int, "id": int})
    assert decoded == state


def test_values_are_coerced_back_to_the_fields_own_type():
    """
    JSON does not remember which of these was an int. Comparing the string "2003" to an integer
    column matches nothing at all and looks exactly like the end of the collection.
    """
    raw = CursorState({"year": "2003", "id": "42"}, query="q").encode()
    decoded = decode_cursor(raw, KEYS, "q", {"year": int, "id": int})
    assert decoded.position == {"year": 2003, "id": 42}


def test_null_stays_null_rather_than_becoming_a_sentinel():
    """A sentinel string is a value some record can legitimately hold; `null` is not."""
    raw = CursorState({"year": None, "id": 1}, query="q").encode()
    assert decode_cursor(raw, KEYS, "q", {"year": int | None, "id": int}).position["year"] is None


def test_a_cursor_from_a_different_query_is_refused():
    raw = CursorState({"year": 1, "id": 1}, query="sorted-by-year").encode()
    with pytest.raises(CursorError, match="different ordering or filter"):
        decode_cursor(raw, KEYS, "sorted-by-title", {})


def test_a_cursor_missing_an_ordering_key_is_refused():
    raw = CursorState({"id": 1}, query="q").encode()
    with pytest.raises(CursorError, match="no value for ordering key"):
        decode_cursor(raw, KEYS, "q", {})


def test_a_corrupt_cursor_is_refused():
    with pytest.raises(CursorError, match="not readable"):
        decode_cursor("!!!not-base64!!!", KEYS, "q", {})


def test_the_fingerprint_follows_the_ordering():
    assert fingerprint((("year", False),), None) != fingerprint((("year", True),), None)
    assert fingerprint((("year", False),), None) != fingerprint((("title", False),), None)


def test_the_fingerprint_follows_the_filter():
    assert fingerprint(KEYS, TrackFilter(year=2000)) != fingerprint(KEYS, TrackFilter(year=2001))
    assert fingerprint(KEYS, TrackFilter()) == fingerprint(KEYS, None)


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------

def predicate(position, *, keys=KEYS, backwards=False, inclusive=False) -> CursorPredicate:
    return CursorPredicate(
        field="", value=position, keys=keys, position=position, backwards=backwards, inclusive=inclusive
    )


def test_the_predicate_compares_the_whole_tuple_not_just_the_first_key():
    """
    A single-key cursor cannot tell these two apart, which is why it needs tie-counting and this
    does not.
    """
    anchor = {"year": 2000, "id": 5}
    assert predicate(anchor).matches(track(6, 2000)) is True
    assert predicate(anchor).matches(track(4, 2000)) is False


def test_the_predicate_excludes_its_own_anchor_unless_inclusive():
    anchor = {"year": 2000, "id": 5}
    assert predicate(anchor).matches(track(5, 2000)) is False
    assert predicate(anchor, inclusive=True).matches(track(5, 2000)) is True


def test_reading_backwards_inverts_the_comparison():
    anchor = {"year": 2000, "id": 5}
    assert predicate(anchor, backwards=True).matches(track(4, 2000)) is True
    assert predicate(anchor, backwards=True).matches(track(6, 2000)) is False


def test_a_descending_key_inverts_it_again():
    keys = (("year", True), ("id", False))
    anchor = {"year": 2000, "id": 5}
    # Descending by year: 1999 sorts *after* 2000.
    assert predicate(anchor, keys=keys).matches(track(1, 1999)) is True
    assert predicate(anchor, keys=keys).matches(track(1, 2001)) is False


def test_mixed_directions_are_handled_per_key():
    keys = (("year", True), ("id", False))
    anchor = {"year": 2000, "id": 5}
    assert predicate(anchor, keys=keys).matches(track(6, 2000)) is True
    assert predicate(anchor, keys=keys).matches(track(4, 2000)) is False


def test_null_sorts_below_every_value_in_both_directions():
    anchor = {"year": 2000, "id": 5}
    assert predicate(anchor).matches(track(1, None)) is False
    assert predicate(anchor, backwards=True).matches(track(1, None)) is True

    null_anchor = {"year": None, "id": 5}
    assert predicate(null_anchor).matches(track(1, 1900)) is True
    assert predicate(null_anchor, backwards=True).matches(track(1, 1900)) is False


# ---------------------------------------------------------------------------
# End to end, over HTTP
# ---------------------------------------------------------------------------

def client_for(database: dict[int, Track]) -> TestClient:
    app = FastAPI()
    router = APIRouter()

    @route_viewset(router, base_path="/tracks", pk_field_name="id")
    class TrackViewSet(CollectionViewSet[int, Track], CursorListMixin[Track, TrackFilter]):
        schema = Track
        default_page_size = 5

        def __init__(self):
            super().__init__(container=database, pk_field="id")

    app.include_router(router)
    return TestClient(app)


def library(count: int) -> dict[int, Track]:
    return {n: Track(id=n, title=f"Track {n:03d}", year=2000 + (n % 3)) for n in range(1, count + 1)}


def walk(client: TestClient, **params) -> list[list[int]]:
    """Follows `next` to the end and returns the ids of every page."""
    pages = []
    cursor = None
    while True:
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        body = client.get("/tracks", params=query).json()
        pages.append([record["id"] for record in body["results"]])
        cursor = body["next"]
        if not cursor:
            return pages


def test_walking_forward_visits_every_record_exactly_once():
    client = client_for(library(23))
    pages = walk(client, sort="id:asc")
    assert [id_ for page in pages for id_ in page] == list(range(1, 24))
    assert [len(page) for page in pages] == [5, 5, 5, 5, 3]


def test_the_last_page_has_no_next():
    client = client_for(library(7))
    body = client.get("/tracks", params={"sort": "id:asc", "limit": 10}).json()
    assert body["next"] is None
    assert body["has_more"] is False


def test_a_multi_key_ordering_with_many_ties_still_visits_each_record_once():
    """
    `year` takes three values across 23 records, so a cursor that stored only the first key would
    have to count its way through the ties. This one does not store only the first key.
    """
    client = client_for(library(23))
    seen = [id_ for page in walk(client, sort="year:asc") for id_ in page]
    assert sorted(seen) == list(range(1, 24))
    assert len(seen) == len(set(seen))


def test_paging_backwards_returns_the_previous_page():
    client = client_for(library(20))
    first_page = client.get("/tracks", params={"sort": "id:asc"}).json()
    second = client.get("/tracks", params={"sort": "id:asc", "cursor": first_page["next"]}).json()
    back = client.get("/tracks", params={"sort": "id:asc", "cursor": second["previous"]}).json()
    assert [record["id"] for record in back["results"]] == [
        record["id"] for record in first_page["results"]
    ]


def test_inserting_a_record_does_not_shift_the_next_page():
    """
    The thing offset paging gets wrong. With `?offset=5` a record inserted before the page boundary
    pushes one record from page 1 onto page 2, where it appears twice.
    """
    database = library(20)
    client = client_for(database)
    page_one = client.get("/tracks", params={"sort": "id:asc"}).json()

    database[0] = Track(id=0, title="inserted at the head", year=2000)

    page_two = client.get("/tracks", params={"sort": "id:asc", "cursor": page_one["next"]}).json()
    assert [record["id"] for record in page_two["results"]] == [6, 7, 8, 9, 10]


def test_the_first_anchor_finds_records_inserted_before_the_page():
    """
    Why the anchor is a row inside the page. Reading back from `first` picks up anything that
    landed in front of it since - and returns `first` itself as well, which is the one duplicate
    the client drops.
    """
    database = {n: Track(id=n, title=f"Track {n}", year=2000) for n in range(10, 20)}
    client = client_for(database)
    page = client.get("/tracks", params={"sort": "id:asc"}).json()
    assert [record["id"] for record in page["results"]] == [10, 11, 12, 13, 14]

    database[9] = Track(id=9, title="just inserted", year=2000)

    caught_up = client.get("/tracks", params={"sort": "id:asc", "cursor": page["first"]}).json()
    ids = [record["id"] for record in caught_up["results"]]
    assert 9 in ids
    assert 10 in ids  # the anchor row itself, returned again


def test_first_and_last_are_present_even_when_there_is_nothing_beyond_them():
    """A null `next` must not take the polling anchor with it."""
    client = client_for(library(3))
    body = client.get("/tracks", params={"sort": "id:asc", "limit": 10}).json()
    assert body["next"] is None
    assert body["previous"] is None
    assert body["first"] is not None
    assert body["last"] is not None


def test_an_empty_collection_answers_with_no_anchors():
    body = client_for({}).get("/tracks").json()
    assert body["results"] == []
    assert body["first"] is None
    assert body["last"] is None


def test_a_cursor_from_a_different_sort_is_rejected_at_the_endpoint():
    client = client_for(library(20))
    page = client.get("/tracks", params={"sort": "id:asc"}).json()
    response = client.get("/tracks", params={"sort": "year:asc", "cursor": page["next"]})
    assert response.status_code == 400
    assert "different ordering" in response.json()["detail"]


def test_paging_composes_with_a_filter():
    client = client_for(library(30))
    pages = walk(client, sort="id:asc", year=2001)
    seen = [id_ for page in pages for id_ in page]
    assert seen == [n for n in range(1, 31) if n % 3 == 1]


def test_the_schema_describes_the_cursor_parameters_and_the_item_type():
    client = client_for(library(3))
    schema = client.get("/tracks/schema").json()
    parameters = {p["name"] for p in schema["paths"]["/tracks"]["get"]["parameters"]}
    assert {"cursor", "limit", "sort"} <= parameters
    assert "offset" not in parameters  # a cursor page has no offset to speak of

    ref = schema["paths"]["/tracks"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    model = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]
    assert model["properties"]["results"]["items"]["$ref"].endswith("/Track")


def test_position_of_reads_the_key_tuple_off_a_record():
    assert position_of(track(7, 1999), KEYS) == {"year": 1999, "id": 7}


# ---------------------------------------------------------------------------
# NULL ordering
# ---------------------------------------------------------------------------

def nullable_client(**attributes) -> TestClient:
    """A library where every third record has no year."""
    app = FastAPI()
    router = APIRouter()
    database = {n: Track(id=n, title=f"T{n:03d}", year=(None if n % 3 == 0 else 2000 + n))
                for n in range(1, 13)}

    def body(namespace):
        namespace.update(attributes)
        namespace["schema"] = Track
        namespace["default_page_size"] = 4
        namespace["__init__"] = lambda self: CollectionViewSet.__init__(
            self, container=database, pk_field="id",
        )

    viewset = types.new_class(
        "NullableViewSet", (CollectionViewSet[int, Track], CursorListMixin[Track, TrackFilter]), {}, body,
    )
    route_viewset(router, base_path="/tracks", pk_field_name="id")(viewset)
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("nulls", ["first", "last"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_a_cursor_walk_over_a_nullable_column_visits_every_row(nulls, direction):
    """
    Regression: the in-memory sort put NULLs at one end and the cursor's comparison assumed the
    other, so a walk silently dropped every row with a NULL - four of twelve, with no error
    anywhere. The two now share one definition of where NULL sits.
    """
    client = nullable_client(nulls=nulls)
    seen, cursor = [], None
    for _ in range(20):
        params = {"sort": f"year:{direction}", "limit": 4}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/tracks", params=params).json()
        seen += [record["id"] for record in body["results"]]
        cursor = body["next"]
        if not cursor:
            break

    assert sorted(seen) == list(range(1, 13))
    assert len(seen) == len(set(seen))


def test_nulls_sit_at_the_end_the_viewset_names_when_ascending():
    first = nullable_client(nulls="first").get("/tracks", params={"sort": "year:asc"}).json()
    assert first["results"][0]["year"] is None

    last = nullable_client(nulls="last").get("/tracks", params={"sort": "year:asc"}).json()
    assert last["results"][0]["year"] is not None


@pytest.mark.parametrize("nulls", ["first", "last"])
def test_the_direction_reverses_which_end_nulls_are_at(nulls):
    """
    `nulls` names the end when ascending. NULL is a value below or above every other, not a row
    pinned to an edge, so descending swaps it - unlike SQL's `NULLS FIRST`, which does not move.
    """
    client = nullable_client(nulls=nulls)
    ascending = client.get("/tracks", params={"sort": "year:asc"}).json()
    descending = client.get("/tracks", params={"sort": "year:desc"}).json()

    assert (ascending["results"][0]["year"] is None) is (nulls == "first")
    assert (descending["results"][0]["year"] is None) is (nulls == "last")
