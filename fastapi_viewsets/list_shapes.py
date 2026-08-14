"""
The shapes a list endpoint can answer in, and how a request picks one.

Three exist, and they are genuinely different contracts rather than variations on a theme:

* `plain` - a bare JSON array. Everything, every time. Fine for a lookup table, wrong for anything
  that grows.
* `paginated` - offset and limit, with a total. Lets a client jump to page 40; re-reads the 39
  pages in front of it to get there, and drifts when rows are inserted between two requests.
* `cursor` - a position rather than a count. Cannot jump to an arbitrary page and cannot report a
  total; in exchange it never re-reads and never drifts.

A viewset declares its default and, optionally, which others a client may ask for by sending
`X-List-Shape`. Declaring only one is the normal case and keeps the OpenAPI schema down to that
single shape - a union nothing asked for is worse documentation than no union at all.
"""

from typing import Any, Generic, Literal, Union

from pydantic import RootModel
from typing_extensions import TypeVar

TItem = TypeVar("TItem")

ListShape = Literal["plain", "paginated", "cursor"]

SHAPE_HEADER = "X-List-Shape"

ALL_SHAPES: tuple[ListShape, ...] = ("plain", "paginated", "cursor")


class ShapeError(ValueError):
    """A viewset asked for a shape that does not exist, or one it did not allow."""


class ListOf(RootModel[list[TItem]], Generic[TItem]):
    """
    A bare JSON array, with a name.

    Serialises to exactly the array it wraps - nothing changes on the wire. The wrapper exists for
    the schema: an inline `{"type": "array"}` has no component for a `$ref` to point at, so both
    doc renderers fall back to the operation's auto-generated title, and a union member ends up
    labelled "Response List Items Music Get" beside two properly named siblings. Wrapped, it is
    `ListOf_MusicTrack_`, which reads like `PaginatedList_MusicTrack_` next to it.
    """


def resolve_shapes(default: ListShape | None, allowed: tuple[str, ...] | None, settings_default: str) -> tuple:
    """
    (default shape, shapes a client may ask for).

    The default is always first in the allowed tuple, and always in it: an endpoint that would
    refuse its own default is a contradiction, and putting it first is what lets the header's
    description name it without a second lookup.
    """
    resolved: ListShape = default or settings_default  # type: ignore[assignment]
    if resolved not in ALL_SHAPES:
        raise ShapeError(f"unknown list shape {resolved!r}; known: {', '.join(ALL_SHAPES)}")

    if allowed is None:
        return resolved, (resolved,)

    unknown = [shape for shape in allowed if shape not in ALL_SHAPES]
    if unknown:
        raise ShapeError(f"unknown list shape(s) {', '.join(map(repr, unknown))}")
    ordered = (resolved, *(shape for shape in allowed if shape != resolved))
    return resolved, ordered


def response_model(shapes: tuple, item_type: Any) -> Any:
    """
    The declared response: one model when the viewset offers one shape, a union when it offers
    several.

    `typing.Union` rather than `X | Y`: the latter builds a `types.UnionType`, which cannot be
    re-subscripted, so TypeVar resolution has no way to substitute the item type into it and the
    schema comes out describing a list of anything.
    """
    from .cursor import CursorPage
    from .list_query import PaginatedList

    models = {"plain": ListOf, "paginated": PaginatedList, "cursor": CursorPage}
    members = tuple(models[shape][item_type] for shape in shapes)
    # typing.Union, not `X | Y`: the latter builds a types.UnionType, which cannot be
    # re-subscripted, so TypeVar resolution could never substitute the item type into it.
    return members[0] if len(members) == 1 else Union[members]  # noqa: UP007
