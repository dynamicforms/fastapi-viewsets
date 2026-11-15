"""
Collations and the in-memory sort and cursor: the sort and the cursor predicate use the same keys, so a
cursor walk visits rows in the order of the sort.
"""

import datetime
import sys
import threading
import warnings

from typing import Any

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from . import collation as collation_module
from .collation import Collation, CollationWarning, get_collation
from .collection_viewset import CollectionViewSet
from .cursor import CursorState, make_predicate
from .decorators import route_viewset
from .list_query import build_list_query
from .mixins import CursorListMixin, ListMixin, SortDirection, SortStateColumn

try:
    import icu  # noqa: F401 - availability probe
except ImportError:
    HAS_ICU = False
else:
    HAS_ICU = True

needs_icu = pytest.mark.skipif(not HAS_ICU, reason="PyICU is not installed (the `icu` extra)")

SAMPLE = [
    "Banana",
    "zebra",
    "apple",
    "Čebula",
    "cesta",
    "Cesta",
    "čas",
    "file10",
    "file2",
    "Žaba",
    "zz",
    "Éclair",
    "eclair",
    "Ša",
    "sa",
    "Sz",
]

DEFAULT_ORDER = [
    "apple",
    "Banana",
    "Cesta",
    "cesta",
    "eclair",
    "file2",
    "file10",
    "sa",
    "Sz",
    "zebra",
    "zz",
    "Éclair",
    "čas",
    "Čebula",
    "Ša",
    "Žaba",
]

SLOVENIAN_ORDER = [
    "apple",
    "Banana",
    "cesta",
    "Cesta",
    "čas",
    "Čebula",
    "eclair",
    "Éclair",
    "file2",
    "file10",
    "sa",
    "Sz",
    "Ša",
    "zebra",
    "zz",
    "Žaba",
]

# A language ICU has no collation data for: it loads the root collation.
UNKNOWN_LOCALE = "xx"


@pytest.fixture(autouse=True)
def fresh_collations(monkeypatch):
    """Every test starts with an empty collation store."""
    monkeypatch.setattr(collation_module, "_collations", {})


@pytest.fixture
def without_icu(monkeypatch):
    """`import icu` raises ImportError."""
    monkeypatch.setitem(sys.modules, "icu", None)


def collation_warnings(call) -> tuple[Any, list[str]]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = call()
    return result, [str(w.message) for w in caught if issubclass(w.category, CollationWarning)]


# ---------------------------------------------------------------------------
# The default collation
# ---------------------------------------------------------------------------


def test_the_default_order_of_the_sample():
    """Code points of the casefolded text, digit runs by value: accented letters sort after `z`."""
    assert sorted(SAMPLE, key=get_collation().sort_key) == DEFAULT_ORDER


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("apple", "Banana"),  # case-insensitive
        ("A", "a"),  # equal when folded: the string breaks the tie
        ("file2", "file10"),  # digit runs by value
        ("a02", "a2"),  # equal value: the string breaks the tie
        ("9", "a"),
        ("", "a"),
        ("٣", "9"),  # any Unicode decimal digit, by value
        ("zz", "Éclair"),  # accents are not folded
        ("strasse", "straße"),  # casefold() equal, the string breaks the tie
        ("straßa", "strasse"),  # `ß` folds to `ss`
        ("fix", "ﬁx"),  # the ligature folds to `fi`
        ("ﬁx", "fiy"),
    ],
)
def test_pairs_in_the_default_order(before, after):
    key = get_collation().sort_key
    assert key(before) < key(after)


def test_keys_of_digit_runs_and_text_never_compare_mixed_types():
    key = get_collation().sort_key
    texts = ["1a", "a1", "a", "1", "", "a1b2", "ab", "a1b", "1a1"]
    assert sorted(texts, key=key) == ["", "1", "1a", "1a1", "a", "a1", "a1b", "a1b2", "ab"]


def test_only_equal_strings_have_equal_keys():
    key = get_collation().sort_key
    assert key("Cesta") != key("cesta")
    assert key("Cesta") == key("Cesta")


def test_a_digit_run_compares_by_value_up_to_640_digits_and_longer_ones_do_not_raise():
    key = get_collation().sort_key
    assert key("a" + "9" * 639) < key("a1" + "0" * 639)
    assert sorted(["a" + "9" * 5000, "a1" + "0" * 5000], key=key)


def test_no_locale_is_the_default_collation_for_every_on_mismatch():
    default = get_collation()
    assert get_collation(None, "fallback") is default
    assert get_collation(None, "use") is default


def test_an_unknown_on_mismatch_is_refused():
    with pytest.raises(ValueError, match="on_mismatch"):
        get_collation("sl", "ignore")  # type: ignore[arg-type]


def test_an_object_with_sort_key_is_a_collation():
    class Reversed:
        def sort_key(self, text: str) -> str:
            return text[::-1]

    assert isinstance(Reversed(), Collation)
    assert isinstance(get_collation(), Collation)


# ---------------------------------------------------------------------------
# ICU collations
# ---------------------------------------------------------------------------


@needs_icu
def test_slovenian_order_of_the_sample():
    collation, messages = collation_warnings(lambda: get_collation("sl"))
    assert sorted(SAMPLE, key=collation.sort_key) == SLOVENIAN_ORDER
    assert messages == []


@needs_icu
@pytest.mark.parametrize("locale", ["sl", "sl_SI", "sl-SI", "en", "en_US", "de@collation=phonebook", "sr_Latn"])
def test_a_locale_of_a_language_icu_collates_is_no_mismatch(locale):
    """English uses ICU's root collation as its own, so it is no mismatch either."""
    collation, messages = collation_warnings(lambda: get_collation(locale, "fallback"))
    assert collation is not get_collation()
    assert messages == []


@needs_icu
@pytest.mark.parametrize(
    ("locale", "same_as"),
    [("iw", "he"), ("in", "id"), ("mo", "ro"), ("sh", "sr_Latn"), ("ji", "yi"), ("tl", "fil"), ("yue", "zh_Hant")],
)
def test_a_legacy_or_alias_code_is_its_canonical_collation_without_a_mismatch(locale, same_as):
    collation, messages = collation_warnings(lambda: get_collation(locale, "fallback"))
    assert collation is not get_collation()
    assert messages == []
    texts = ["b", "a", "č", "c", "ש", "א", "中", "文"]
    assert sorted(texts, key=collation.sort_key) == sorted(texts, key=get_collation(same_as).sort_key)


@needs_icu
@pytest.mark.parametrize("first", ["nb", "nn"])
def test_norwegian_is_no_mismatch_after_a_bokmal_or_nynorsk_collation(first):
    get_collation(first, "use")
    collation, messages = collation_warnings(lambda: get_collation("no", "fallback"))
    assert collation is not get_collation()
    assert messages == []


@needs_icu
@pytest.mark.parametrize("locale", ["", "root", "und", "und_US"])
def test_a_root_locale_is_the_icu_root_collation_without_a_mismatch(locale):
    collation, messages = collation_warnings(lambda: get_collation(locale, "fallback"))
    assert collation is not get_collation()
    assert messages == []


@needs_icu
@pytest.mark.parametrize("locale", ["@@@", "-", "x" * 200])
@pytest.mark.parametrize("on_mismatch", ["use", "warn", "fallback"])
def test_a_string_that_is_no_locale_is_refused(locale, on_mismatch):
    with pytest.raises(ValueError, match="is not a locale"):
        get_collation(locale, on_mismatch)
    assert collation_module._collations == {}


@needs_icu
def test_the_same_locale_and_on_mismatch_return_the_same_instance():
    assert get_collation("sl") is get_collation("sl")
    assert get_collation("sl", "use") is not get_collation("sl")


@needs_icu
def test_a_mismatch_with_use_takes_the_root_collation_silently():
    collation, messages = collation_warnings(lambda: get_collation(UNKNOWN_LOCALE, "use"))
    assert collation is not get_collation()
    assert sorted(["b", "č", "c"], key=collation.sort_key) == ["b", "c", "č"]
    assert messages == []


@needs_icu
def test_a_mismatch_with_warn_takes_the_root_collation_and_warns_once():
    collation, messages = collation_warnings(lambda: get_collation(UNKNOWN_LOCALE))
    assert collation is not get_collation()
    assert messages == ["ICU has no collation for 'xx': collation 'xx' is the ICU root collation"]
    again, messages = collation_warnings(lambda: get_collation(UNKNOWN_LOCALE, "warn"))
    assert again is collation
    assert messages == []


@needs_icu
def test_a_mismatch_with_fallback_takes_the_default_collation_and_warns_once():
    collation, messages = collation_warnings(lambda: get_collation(UNKNOWN_LOCALE, "fallback"))
    assert collation is get_collation()
    assert messages == ["ICU has no collation for 'xx': collation 'xx' is the default"]
    _, messages = collation_warnings(lambda: get_collation(UNKNOWN_LOCALE, "fallback"))
    assert messages == []


@needs_icu
def test_another_thread_uses_its_own_collator_with_the_same_keys():
    collation = get_collation("sl")
    found = {}

    def other_thread():
        found["key"] = collation.sort_key("Čebula")
        found["collator"] = collation._threads.collator

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()
    assert found["key"] == collation.sort_key("Čebula")
    assert found["collator"] is not collation._threads.collator


@needs_icu
def test_equal_icu_keys_are_broken_by_the_string():
    key = get_collation("sl").sort_key
    assert key("á") != key("á")  # canonically equivalent: equal ICU keys
    assert key("á")[0] == key("á")[0]


@pytest.mark.usefixtures("without_icu")
def test_without_pyicu_a_locale_is_the_default_collation_with_one_warning():
    collation, messages = collation_warnings(lambda: get_collation("sl", "use"))
    assert collation is get_collation()
    assert messages == ["PyICU is not installed: collation 'sl' is the default"]
    _, messages = collation_warnings(lambda: get_collation("sl", "use"))
    assert messages == []


def test_concurrent_first_calls_create_one_instance(monkeypatch):
    created = []

    def create(locale, on_mismatch):  # noqa: ARG001
        created.append(locale)
        return collation_module._DefaultCollation()

    monkeypatch.setattr(collation_module, "_create", create)
    barrier = threading.Barrier(8)
    results = []

    def call():
        barrier.wait()
        results.append(get_collation("sl"))

    threads = [threading.Thread(target=call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert created == ["sl"]
    assert all(result is results[0] for result in results)


# ---------------------------------------------------------------------------
# sort_list
# ---------------------------------------------------------------------------


class Word(BaseModel):
    id: int
    text: str | None
    code: str | None = None
    score: Any = None


def words(texts: list[str | None]) -> dict[int, Word]:
    return {n: Word(id=n, text=text, code=text) for n, text in enumerate(texts, start=1)}


class CodePoint:
    def sort_key(self, text: str) -> str:
        return text


def make_viewset(database: dict[int, Word], **attributes):
    class WordViewSet(CollectionViewSet[int, Word], CursorListMixin[Word, None]):
        schema = Word
        default_page_size = 3

        def __init__(self):
            super().__init__(container=database, pk_field="id")

    for name, value in attributes.items():
        setattr(WordViewSet, name, value)
    return WordViewSet


async def sort(viewset_class, column: str, direction: str = "asc") -> list[Word]:
    columns = [SortStateColumn(column_name=column, direction=SortDirection(direction))]
    return await viewset_class().sort_list(columns, list(viewset_class().container.values()))


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_sort_list_uses_the_default_collation(direction):
    result = await sort(make_viewset(words(SAMPLE)), "text", direction)
    expected = DEFAULT_ORDER if direction == "asc" else DEFAULT_ORDER[::-1]
    assert [word.text for word in result] == expected


@needs_icu
@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_sort_list_uses_the_viewset_collation(direction):
    result = await sort(make_viewset(words(SAMPLE), collation=get_collation("sl")), "text", direction)
    expected = SLOVENIAN_ORDER if direction == "asc" else SLOVENIAN_ORDER[::-1]
    assert [word.text for word in result] == expected


@pytest.mark.asyncio
async def test_collation_for_sets_the_order_per_column():
    async def collation_for(self, context, column_name):  # noqa: ARG001
        return CodePoint() if column_name == "code" else self.collation

    viewset = make_viewset(words(SAMPLE), collation_for=collation_for)
    assert [word.code for word in await sort(viewset, "code")] == sorted(SAMPLE)
    assert [word.text for word in await sort(viewset, "text")] == DEFAULT_ORDER


@pytest.mark.asyncio
async def test_collation_for_receives_the_list_context():
    seen = []
    context = object()

    async def collation_for(self, context, column_name):
        seen.append((context, column_name))
        return self.collation

    viewset_class = make_viewset(words(SAMPLE), collation_for=collation_for)
    first = await viewset_class().get_list(context, build_list_query(None, "text:asc", limit=4))
    query = build_list_query(None, "text:asc", limit=4)
    query.cursor = first.next
    await viewset_class().get_list(context, query)
    assert seen
    assert {entry[0] for entry in seen} == {context}
    assert {entry[1] for entry in seen} == {"text", "id"}

    seen.clear()
    await sort(viewset_class, "text")
    assert seen == [(None, "text")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("nulls", "first"), [("first", None), ("last", "a")])
async def test_the_collation_never_sees_null_and_null_placement_holds(nulls, first):
    class Folding:
        def sort_key(self, text):
            assert text is not None
            return text.casefold()

    viewset = make_viewset(words(["B", None, "a"]), collation=Folding(), nulls=nulls)
    result = await sort(viewset, "text")
    assert result[0].text == first


@pytest.mark.asyncio
async def test_non_string_values_sort_as_they_are():
    class Raising:
        def sort_key(self, text):
            raise AssertionError(f"sort_key called for {text!r}")

    database = {
        1: Word(id=1, text="x", score=10),
        2: Word(id=2, text="x", score=9),
        3: Word(id=3, text="x", score=-1.5),
    }
    assert [w.score for w in await sort(make_viewset(database, collation=Raising()), "score")] == [-1.5, 9, 10]

    dates = {n: Word(id=n, text="x", score=datetime.date(2026, 1, 10 - n)) for n in range(1, 4)}
    assert [w.id for w in await sort(make_viewset(dates), "score")] == [3, 2, 1]


@pytest.mark.asyncio
async def test_mixed_types_do_not_raise():
    database = {n: Word(id=n, text="x", score=score) for n, score in enumerate(["b", 3, "A"], start=1)}
    result = await sort(make_viewset(database), "score")
    assert len(result) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("nulls", ["first", "last"])
async def test_sort_list_orders_by_several_columns_like_compare_in_order(nulls):
    """Mixed directions and NULLs in the first column; the second column breaks its ties."""
    database = {
        n: Word(id=n, text=text, score=score)
        for n, (text, score) in enumerate(
            [("b", 2), (None, 1), ("a", 2), ("B", 1), (None, 2), ("a", 1), ("Č", None)], start=1
        )
    }
    viewset = make_viewset(database, nulls=nulls)()
    columns = [
        SortStateColumn(column_name="score", direction=SortDirection.desc),
        SortStateColumn(column_name="text", direction=SortDirection.asc),
    ]
    result = [(w.score, w.text) for w in await viewset.sort_list(columns, list(database.values()))]
    tail = [(2, "a"), (2, "b"), (2, None), (1, "a"), (1, "B"), (1, None)]
    if nulls == "first":
        assert result == [(2, None), (2, "a"), (2, "b"), (1, None), (1, "a"), (1, "B"), (None, "Č")]
    else:
        assert result == [(None, "Č"), *tail]


@pytest.mark.asyncio
async def test_a_plain_list_mixin_uses_the_default_collation():
    assert ListMixin.collation is get_collation()
    assert await ListMixin.collation_for(ListMixin, None, "text") is get_collation()


# ---------------------------------------------------------------------------
# Cursor paging
# ---------------------------------------------------------------------------


def client_for(viewset_class) -> TestClient:
    app = FastAPI()
    router = APIRouter()
    route_viewset(router, base_path="/words", pk_field_name="id")(viewset_class)
    app.include_router(router)
    return TestClient(app)


def walk(client: TestClient, sort: str, path: str = "/words") -> list[str]:
    """Follows `next` to the end, then `previous` back to the start; both must read the same rows."""
    pages, cursor = [], None
    for _ in range(50):
        params = {"sort": sort, "limit": 3, **({"cursor": cursor} if cursor else {})}
        body = client.get(path, params=params).json()
        pages.append([record["text"] for record in body["results"]])
        cursor = body["next"]
        if not cursor:
            break
    else:
        raise AssertionError("the walk did not end")
    forwards = [text for page in pages for text in page]

    backwards, cursor = list(pages[-1]), body["previous"]
    while cursor:
        body = client.get(path, params={"sort": sort, "limit": 3, "cursor": cursor}).json()
        backwards = [record["text"] for record in body["results"]] + backwards
        cursor = body["previous"]
    assert backwards == forwards
    return forwards


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_a_cursor_walk_follows_the_default_collation_both_ways(direction):
    client = client_for(make_viewset(words(SAMPLE)))
    expected = DEFAULT_ORDER if direction == "asc" else DEFAULT_ORDER[::-1]
    assert walk(client, f"text:{direction}") == expected


@needs_icu
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_a_cursor_walk_follows_an_icu_collation_both_ways(direction):
    client = client_for(make_viewset(words(SAMPLE), collation=get_collation("sl")))
    expected = SLOVENIAN_ORDER if direction == "asc" else SLOVENIAN_ORDER[::-1]
    assert walk(client, f"text:{direction}") == expected


@pytest.mark.parametrize("nulls", ["first", "last"])
def test_a_cursor_walk_visits_null_rows(nulls):
    texts = ["b", None, "Č", "a", None, "c", "D"]
    seen = walk(client_for(make_viewset(words(texts), nulls=nulls)), "text:asc")
    letters = ["a", "b", "c", "D", "Č"]
    assert seen == ([None, None, *letters] if nulls == "first" else [*letters, None, None])


@pytest.mark.asyncio
async def test_the_cursor_predicate_compares_collation_keys():
    viewset_class = make_viewset(words(SAMPLE))
    first = await viewset_class().get_list(None, build_list_query(None, "text:asc", limit=4))
    query = build_list_query(None, "text:asc", limit=4)
    query.cursor = first.next
    await viewset_class().get_list(None, query)
    (predicate,) = query.extra_filters
    assert predicate.sort_value("text", "čas") == get_collation().sort_key("čas")
    assert predicate.sort_value("text", 5) == 5


def test_the_cursor_predicate_computes_the_anchor_keys_once():
    calls = []

    def sort_value(name, value):
        calls.append((name, value))
        return value

    keys = (("text", False), ("id", False))
    predicate = make_predicate(CursorState(position={"text": "m", "id": 5}), keys, sort_value=sort_value)
    assert calls == []
    records = [{"text": text, "id": n} for n, text in enumerate(["a", "z", "m", "b", "m"], start=3)]
    assert [predicate.matches(record) for record in records] == [False, True, False, False, True]
    assert calls.count(("text", "m")) == 1 + 2  # the anchor once, the two records with "m"
    assert calls.count(("id", 5)) == 1 + 1  # the anchor once, the record with id 5


class Tag(BaseModel):
    code: str
    text: str


def test_a_collation_that_merges_primary_keys_does_not_skip_rows():
    """The raw primary key breaks ties between equal collation keys of the primary key."""
    database = {code: Tag(code=code, text="x") for code in ["a", "A", "b", "B", "c"]}

    class Folding:
        def sort_key(self, text):
            return text.casefold()

    class TagViewSet(CollectionViewSet[str, Tag], CursorListMixin[Tag, None]):
        schema = Tag
        collation = Folding()

        def __init__(self):
            super().__init__(container=database, pk_field="code")

    app = FastAPI()
    router = APIRouter()
    route_viewset(router, base_path="/tags", pk_field_name="code")(TagViewSet)
    app.include_router(router)
    client = TestClient(app)

    seen, cursor = [], None
    for _ in range(20):
        params = {"sort": "text:asc", "limit": 1, **({"cursor": cursor} if cursor else {})}
        body = client.get("/tags", params=params).json()
        seen += [record["code"] for record in body["results"]]
        cursor = body["next"]
        if not cursor:
            break
    assert seen == ["A", "a", "B", "b", "c"]
