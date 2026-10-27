"""
Cursor pagination: the next page is "every row past this one" rather than "skip N rows", so it
neither re-reads skipped rows nor drifts when rows are inserted or deleted between requests.

A cursor carries the full ordering key tuple of a row; the primary key is appended to the ordering,
so every tuple is unique and ties need no handling. Anchors are the page's own edge rows, so rows
inserted next to the page are not skipped when paging back.
"""

import base64
import binascii
import hashlib
import json

from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TYPE_CHECKING

from pydantic import BaseModel, TypeAdapter
from typing_extensions import TypeVar

from .error_namespace import _namespaced
from .filters import Filter

if TYPE_CHECKING:
    from .mixins import SortState

TItem = TypeVar("TItem")


class CursorError(ValueError):
    """The cursor is unreadable or belongs to another query. `code` is `fastapi-viewsets.cursor_<reason>`."""

    def __init__(self, message: str, code: str, params: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.params = params or {}


CursorKeys = tuple[tuple[str, bool], ...]
"""Ordering keys as (field name, descending), primary key last. The shape a position is read against."""


def cursor_keys(sort: "SortState", pk_field: str) -> CursorKeys:
    """The client's ordering with `pk_field` appended (when absent), making every key tuple unique."""
    from .mixins import SortDirection

    keys = tuple((column.column_name, column.direction == SortDirection.desc) for column in sort)
    if not any(name == pk_field for name, _ in keys):
        keys += ((pk_field, False),)
    return keys


def fingerprint(keys: CursorKeys, fltr: Any) -> str:
    """
    Short hash of the ordering keys and active filters, tying a cursor to its query. It detects
    mistakes, not tampering: cursors are not signed.
    """
    material: dict[str, Any] = {"keys": [[name, desc] for name, desc in keys]}
    active = (
        {name: _jsonable(value) for name, value in sorted(fltr.model_dump().items()) if value is not None}
        if fltr is not None and hasattr(fltr, "model_dump")
        else {}
    )
    # An all-None filter model must fingerprint the same as no filter model.
    if active:
        material["fltr"] = active
    encoded = json.dumps(material, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


@dataclass(frozen=True)
class CursorState:
    """
    Where a cursor points and how to read from there. `inclusive` returns the anchor row again,
    giving a polling client a stable point to re-ask from.
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
    Decodes a cursor and coerces its values to the field types in `annotations` (a string "2003"
    would match no integer row). Raises CursorError when it is unreadable or belongs to another query.
    """
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error) as error:
        raise CursorError(
            f"cursor is not readable: {error}", code=_namespaced("cursor_unreadable"), params={"error": str(error)}
        ) from None

    if not isinstance(payload, dict) or not isinstance(payload.get("p"), dict):
        raise CursorError("cursor is not readable: no position in it", code=_namespaced("cursor_missing_position"))

    if payload.get("q") != expected_query:
        raise CursorError(
            "this cursor was issued for a different ordering or filter - start from the first page",
            code=_namespaced("cursor_stale"),
        )

    position = payload["p"]
    missing = [name for name, _ in keys if name not in position]
    if missing:
        raise CursorError(
            f"cursor has no value for ordering key(s): {', '.join(missing)}",
            code=_namespaced("cursor_missing_keys"),
            params={"missing": missing},
        )

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
            raise CursorError(
                f"cursor value for {name!r} does not fit the field: {error}",
                code=_namespaced("cursor_value_mismatch"),
                params={"name": name, "error": str(error)},
            ) from None

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
    One cursor page. `next`/`previous` are exclusive and null when nothing lies that way; `first`/`last`
    are the same anchors read inclusively, present whenever the page is non-empty, for polling an edge.
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
    "Every row past this position" as a filter, so backends compile it like any other filter.
    The inherited `field` and `value` are unused.
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
    Whether this key's value must be greater than the anchor's: descending and backwards each invert it.
    Shared by the in-memory path and backend compilers.
    """
    return (not descending) != backwards


def compare_in_order(left: Any, right: Any, descending: bool = False, nulls_first: bool = True) -> int:
    """
    Three-way comparison in sort order, NULL being the lowest (`nulls_first`) or highest value.
    `nulls_first` applies when ascending; descending reverses it, unlike SQL `NULLS FIRST/LAST`, so a
    backend flips the placement for a descending key. Incomparable values compare equal.
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
    """JSON-native values as they are, anything else (date, Decimal, UUID) as `str()`; decoding coerces back."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
