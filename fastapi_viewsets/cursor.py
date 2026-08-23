"""
Cursor pagination: paging by where you are rather than by how many you skipped.

Offset paging re-reads every skipped row and drifts whenever records are inserted or deleted
between two requests - page 2 can repeat a row that page 1 already showed, or skip one entirely.
A cursor carries the ordering keys of an actual row instead, so the next page is "everything past
this row" and neither problem exists.

Three decisions shape everything here.

**The cursor carries the whole key tuple, not just the first key.** DRF's cursor stores only the
first ordering field, which makes it wrong for any genuinely multi-key ordering and forces it to
handle ties by counting rows within a run of equal values - with a hard cap that collapses on a
low-cardinality column. Storing the full tuple removes ties instead of coping with them.

**The primary key joins the ordering automatically.** With it every key tuple is unique, so there
is exactly one row at any position and no tie-breaking machinery is needed at all.

**Anchors are rows inside the page, never the rows on either side of it.** This is the subtle one.
DRF encodes the position of the record *before* the page into `previous`; anything inserted
between that record and the page's own first row then sits in a gap that paging backwards skips
entirely, because the boundary moved. A row that is in the page cannot move like that, so `first`
and `last` anchor on the page's own edges and the gap does not exist.
"""

import base64
import binascii
import hashlib
import json

from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TYPE_CHECKING

from pydantic import BaseModel, TypeAdapter
from typing_extensions import TypeVar

from .filters import Filter

if TYPE_CHECKING:
    from .mixins import SortState

TItem = TypeVar("TItem")


class CursorError(ValueError):
    """The cursor is unreadable, or does not belong to the query it was sent with."""


CursorKeys = tuple[tuple[str, bool], ...]
"""Ordering keys as (field name, descending), primary key last. The shape a position is read against."""


def cursor_keys(sort: "SortState", pk_field: str) -> CursorKeys:
    """
    The client's ordering with the primary key appended.

    Appended rather than substituted, and only when absent: it is what makes every key tuple
    unique, which is what lets ties be ignored rather than counted. Uses the viewset's actual
    primary key name - assuming `id` breaks on any model that renamed it.
    """
    from .mixins import SortDirection

    keys = tuple((column.column_name, column.direction == SortDirection.desc) for column in sort)
    if not any(name == pk_field for name, _ in keys):
        keys += ((pk_field, False),)
    return keys


def fingerprint(keys: CursorKeys, fltr: Any) -> str:
    """
    Ties a cursor to the query it was issued for.

    A position is only meaningful against the ordering it was read in: hand the same cursor to a
    differently sorted query and the key names may not even exist in it. Filters are included too,
    because a cursor pasted into a different filter set describes a page nobody asked for. Eight
    bytes is plenty - this catches mistakes, not attacks; see the note on signing below.
    """
    material: dict[str, Any] = {"keys": [[name, desc] for name, desc in keys]}
    active = (
        {name: _jsonable(value) for name, value in sorted(fltr.model_dump().items()) if value is not None}
        if fltr is not None and hasattr(fltr, "model_dump")
        else {}
    )
    # Only when something is actually set: a filter model of all Nones is the same request as no
    # filter model at all, and must fingerprint the same or a cursor stops working the moment
    # FastAPI starts building an empty one.
    if active:
        material["fltr"] = active
    encoded = json.dumps(material, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


@dataclass(frozen=True)
class CursorState:
    """
    Where a cursor points, and how to read from there.

    `inclusive` is what makes an anchor usable for polling. Reading strictly past a row cannot
    miss anything inserted beyond it, so `next`/`previous` are exclusive; reading *from* a row
    inclusive returns that row again - one duplicate the caller drops - and in exchange gives a
    client a stable point to re-ask from without having to remember anything else.
    """

    position: dict[str, Any]
    backwards: bool = False
    inclusive: bool = False
    query: str = ""

    def encode(self) -> str:
        payload = {
            "p": self.position,
            "b": self.backwards,
            "i": self.inclusive,
            "q": self.query,
        }
        raw = json.dumps(payload, separators=(",", ":"), default=_jsonable).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(raw: str, keys: CursorKeys, expected_query: str, annotations: dict[str, Any]) -> CursorState:
    """
    Reads a cursor back, and refuses one that does not belong here.

    Values are coerced to the field's own type on the way in. They arrive as whatever JSON made of
    them, and comparing the string "2003" against an integer column matches nothing at all while
    looking like an empty page - so the coercion is not a nicety.
    """
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error) as error:
        raise CursorError(f"cursor is not readable: {error}") from None

    if not isinstance(payload, dict) or not isinstance(payload.get("p"), dict):
        raise CursorError("cursor is not readable: no position in it")

    if payload.get("q") != expected_query:
        raise CursorError("this cursor was issued for a different ordering or filter - start from the first page")

    position = payload["p"]
    missing = [name for name, _ in keys if name not in position]
    if missing:
        raise CursorError(f"cursor has no value for ordering key(s): {', '.join(missing)}")

    coerced: dict[str, Any] = {}
    for name, _ in keys:
        value = position[name]
        annotation = annotations.get(name)
        if value is None or annotation is None:
            coerced[name] = value
            continue
        try:
            coerced[name] = TypeAdapter(annotation).validate_python(value)
        except Exception as error:
            raise CursorError(f"cursor value for {name!r} does not fit the field: {error}") from None

    return CursorState(
        position=coerced,
        backwards=bool(payload.get("b")),
        inclusive=bool(payload.get("i")),
        query=expected_query,
    )


def position_of(record: Any, keys: CursorKeys) -> dict[str, Any]:
    """The key tuple of one row, as it goes into a cursor."""
    return {name: _jsonable(_read(record, name)) for name, _ in keys}


class CursorPage(BaseModel, Generic[TItem]):
    """
    One page, with four cursors built from two anchors.

    `next` and `previous` are the ordinary links, exclusive so they never repeat a row. `first` and
    `last` are the same two anchors read inclusively: they return their own row again, and in
    exchange they are stable points a client can re-ask from to pick up whatever has been inserted
    at that edge since. Unlike `next`/`previous` they are present whenever the page is non-empty,
    regardless of whether there is more data in that direction - which is exactly what a client
    polling the head of a list needs and what a null `next` takes away.
    """

    results: list[TItem]
    limit: int
    has_more: bool = False
    has_previous: bool = False
    next: str | None = None
    previous: str | None = None
    first: str | None = None
    last: str | None = None


@dataclass(frozen=True)
class CursorPredicate(Filter):
    """
    "Every row past this one, in this ordering" - as a filter, so it reaches a backend through the
    same registry every other filter uses and falls back to `matches()` when none can translate it.

    `field` and `value` are inherited and unused; the keys and the position carry everything.
    """

    lookup: ClassVar[str] = "cursor"

    keys: CursorKeys = ()
    position: dict[str, Any] = None  # noqa: RUF009 - frozen dataclass, never mutated
    backwards: bool = False
    inclusive: bool = False
    nulls_first: bool = True

    def fields(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.keys)

    def matches(self, record: Any) -> bool:
        for name, descending in self.keys:
            order = compare_in_order(
                _read(record, name),
                self.position.get(name),
                descending,
                self.nulls_first,
            )
            if order != 0:
                # `order > 0` means the row sorts after the anchor; reading backwards wants before.
                return order < 0 if self.backwards else order > 0
        return self.inclusive  # every key equal: this is the anchor row itself


def make_predicate(state: CursorState, keys: CursorKeys, nulls_first: bool = True) -> CursorPredicate:
    return CursorPredicate(
        field="",
        value=state.position,
        keys=keys,
        position=state.position,
        backwards=state.backwards,
        inclusive=state.inclusive,
        nulls_first=nulls_first,
    )


def wants_greater(descending: bool, backwards: bool) -> bool:
    """
    Whether this key's raw value must be greater than the anchor's.

    Two independent reversals: a descending key inverts the comparison, and reading backwards
    inverts it again. Shared by the in-memory path and every backend compiler so the two cannot
    disagree about what a cursor means.
    """
    return (not descending) != backwards


def compare_in_order(left: Any, right: Any, descending: bool = False, nulls_first: bool = True) -> int:
    """
    Three-way comparison in sort order, with NULL sitting at one end of the value range.

    `nulls_first` says which end **when ascending**; reversing the direction reverses it, because
    NULL is being treated as a value smaller or larger than every other, not as a row pinned to an
    edge. This is not SQL's `NULLS FIRST`/`NULLS LAST`, which stay put when the direction changes -
    a backend translating this has to flip the placement it emits for a descending key.

    SQL makes every comparison with NULL unknown; a definite answer is the only way a nullable
    column can take part in the total order a cursor is built on. `sort_list` compares with this
    too, so the rows a cursor steps through and the rows the in-memory sort produces cannot
    disagree about where NULLs went.
    """
    if left is None and right is None:
        order = 0
    elif left is None:
        order = -1 if nulls_first else 1
    elif right is None:
        order = 1 if nulls_first else -1
    else:
        try:
            order = (left > right) - (left < right)
        except TypeError:
            return 0
    return -order if descending else order


def _read(record: Any, name: str) -> Any:
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def _jsonable(value: Any) -> Any:
    """
    JSON-native where possible, `str()` otherwise.

    Deliberately real JSON rather than stringifying everything: `null` stays `null` instead of
    needing a sentinel string, which is a value a record could legitimately hold. Anything JSON
    cannot carry (a date, a Decimal, a UUID) goes out as a string and is coerced back through the
    field's own annotation on the way in.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
