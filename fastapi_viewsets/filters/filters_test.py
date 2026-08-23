from dataclasses import dataclass
from typing import ClassVar

import pytest

from pydantic import BaseModel

from ..list_query import ListQuery
from ..mixins import ListMixin
from .base import Filter, FilterError, FilterSet
from .declaration import filters_from, make_filter_model
from .operators import Exact, Gte, IContains, In, IsNull, known_operators, operator_for, Overlaps, register_operator
from .registry import can_compile_all, compile_all, compiler_for, compiles


class Track(BaseModel):
    id: int
    title: str
    artist: str | None
    year: int
    genres: list[str]


LIBRARY = [
    Track(id=1, title="Kind of Blue", artist="Miles Davis", year=1959, genres=["jazz"]),
    Track(id=2, title="Blue Train", artist="John Coltrane", year=1957, genres=["jazz", "bop"]),
    Track(id=3, title="Blue Monday", artist=None, year=1983, genres=["electronic"]),
]


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


def test_exact_and_icontains():
    assert Exact("year", 1959).matches(LIBRARY[0])
    assert not Exact("year", 1960).matches(LIBRARY[0])
    assert IContains("title", "BLUE").matches(LIBRARY[0])


def test_comparisons_treat_a_missing_value_as_no_match_rather_than_an_error():
    """A nullable column is normal; asking for artist >= 'M' should not crash on the rows with none."""
    assert Gte("artist", "M").matches(LIBRARY[0])
    assert Gte("artist", "M").matches(LIBRARY[2]) is False


def test_in_and_isnull():
    assert In("year", [1957, 1959]).matches(LIBRARY[1])
    assert not In("year", [1980]).matches(LIBRARY[1])
    assert IsNull("artist", True).matches(LIBRARY[2])
    assert IsNull("artist", False).matches(LIBRARY[0])


def test_overlaps_matches_on_any_shared_member():
    assert Overlaps("genres", ["bop", "rock"]).matches(LIBRARY[1])
    assert not Overlaps("genres", ["rock"]).matches(LIBRARY[1])


def test_filters_read_from_mappings_as_well_as_objects():
    assert Exact("year", 1959).matches({"year": 1959})
    assert not Exact("year", 1959).matches({})


# ---------------------------------------------------------------------------
# FilterSet
# ---------------------------------------------------------------------------


def test_a_filter_set_requires_every_filter_to_match():
    matching = FilterSet([IContains("title", "blue"), Exact("year", 1957)])
    assert matching.apply(LIBRARY) == [LIBRARY[1]]


def test_an_empty_filter_set_is_falsy():
    assert not FilterSet([])
    assert FilterSet([Exact("year", 1)])


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


def test_the_declaration_decides_the_query_parameters():
    model = make_filter_model(Track, {"year": ["exact", "gte"], "title": ["icontains"]})
    assert set(model.model_fields) == {"year", "year__gte", "title__icontains"}


def test_exact_keeps_the_bare_field_name():
    """`?year=2003` is what anyone would write; `?year__exact=2003` is what nobody would."""
    assert "year" in make_filter_model(Track, {"year": ["exact"]}).model_fields


def test_every_parameter_is_optional():
    model = make_filter_model(Track, {"year": ["exact", "gte"]})
    assert model().year is None


def test_in_takes_a_comma_separated_string_and_coerces_it():
    """
    FastAPI drops a list-typed field from a Depends()-expanded model, so multi-valued operators
    travel as one string. The values still have to end up as the field's own type - comparing the
    string "1957" to an integer column would quietly match nothing.
    """
    model = make_filter_model(Track, {"year": ["in"]})
    filter_set = filters_from(model(year__in="1957, 1959"))
    assert filter_set.filters[0].value == [1957, 1959]


def test_a_list_value_that_will_not_convert_is_refused_not_dropped():
    model = make_filter_model(Track, {"year": ["in"]})
    with pytest.raises(FilterError, match="cannot read"):
        filters_from(model(year__in="1957,not-a-year"))


def test_overlaps_takes_the_list_fields_item_type():
    model = make_filter_model(Track, {"genres": ["overlaps"]})
    filter_set = filters_from(model(genres__overlaps="jazz,bop"))
    assert filter_set.filters[0].value == ["jazz", "bop"]
    assert filter_set.apply(LIBRARY) == [LIBRARY[0], LIBRARY[1]]


def test_isnull_takes_a_bool_whatever_the_field_is():
    model = make_filter_model(Track, {"year": ["isnull"]})
    assert model(year__isnull=True).year__isnull is True


def test_an_unknown_field_is_refused_at_declaration_time():
    """
    Silently ignoring it would produce a parameter that never matches anything - the kind of bug
    found in production by someone wondering why the search box does nothing.
    """
    with pytest.raises(FilterError, match="no field 'nope'"):
        make_filter_model(Track, {"nope": ["exact"]})


def test_an_unknown_operator_is_refused_at_declaration_time():
    with pytest.raises(FilterError, match="unknown filter operator"):
        make_filter_model(Track, {"year": ["approximately"]})


def test_filters_from_builds_only_the_parameters_that_were_sent():
    model = make_filter_model(Track, {"year": ["exact", "gte"], "title": ["icontains"]})
    filter_set = filters_from(model(year__gte=1958))
    assert len(filter_set) == 1
    assert isinstance(filter_set.filters[0], Gte)
    assert filter_set.filters[0].field == "year"


def test_filters_from_returns_none_for_a_hand_made_model():
    """That is what keeps the older filter_list path working untouched."""

    class HandMade(BaseModel):
        year: int | None = None

    assert filters_from(HandMade(year=1959)) is None


def test_operator_lookup_reports_what_it_knows():
    assert operator_for("gte") is Gte
    assert "icontains" in known_operators()
    with pytest.raises(KeyError, match="unknown filter operator"):
        operator_for("nope")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class FakeBackend:
    pass


class DerivedBackend(FakeBackend):
    pass


@compiles(FakeBackend, Exact)
def _compile_exact(_viewset, fltr, query):
    return query + [f"{fltr.field}={fltr.value}"]


def test_a_compiler_is_inherited_by_a_derived_backend():
    """Otherwise every subclass of a backend would have to re-register everything."""
    assert compiler_for(DerivedBackend, Exact) is _compile_exact


def test_an_unregistered_filter_has_no_compiler():
    assert compiler_for(FakeBackend, IContains) is None


def test_can_compile_all_is_all_or_nothing():
    assert can_compile_all(FakeBackend, [Exact("a", 1), Exact("b", 2)])
    assert not can_compile_all(FakeBackend, [Exact("a", 1), IContains("b", "x")])


def test_compile_all_threads_the_query_through_every_filter():
    assert compile_all(FakeBackend, None, [Exact("a", 1), Exact("b", 2)], []) == ["a=1", "b=2"]


# ---------------------------------------------------------------------------
# End to end through the pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_declared_filter_needs_no_filter_list_at_all():
    """
    The point of the whole thing: the viewset declares which operators its fields accept and writes
    no filtering code.
    """
    # noqa: N806 - PascalCase is right for a generated class, whatever it is assigned to
    TrackFilter = make_filter_model(Track, {"title": ["icontains"], "year": ["gte"]})  # noqa: N806

    class TrackViewSet(ListMixin[Track, TrackFilter]):
        async def perform_list(self, _context):
            return list(LIBRARY)

    result = await TrackViewSet().get_list(None, ListQuery(fltr=TrackFilter(title__icontains="blue", year__gte=1958)))
    assert [track.id for track in result] == [1, 3]


@pytest.mark.asyncio
async def test_a_third_party_operator_works_with_no_backend_changes():
    @register_operator
    @dataclass(frozen=True)
    class DecadeOf(Filter):
        lookup: ClassVar[str] = "decade"

        def matches(self, record) -> bool:
            actual = self.read(record)
            return actual is not None and actual // 10 * 10 == self.value

    TrackFilter = make_filter_model(Track, {"year": ["decade"]})  # noqa: N806 - a generated class

    class TrackViewSet(ListMixin[Track, TrackFilter]):
        async def perform_list(self, _context):
            return list(LIBRARY)

    result = await TrackViewSet().get_list(None, ListQuery(fltr=TrackFilter(year__decade=1950)))
    assert [track.id for track in result] == [1, 2]
