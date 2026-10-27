"""
The shapes a list endpoint answers in: `plain` (bare array), `paginated` (offset/limit with a total)
and `cursor` (position-based, no total). A viewset declares its default and optionally the others a
client may request with `X-List-Shape`; only the declared shapes appear in the OpenAPI schema.
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


def _placeholder(schema: dict, definitions: dict, depth: int = 0) -> Any:
    """One made-up value matching a JSON schema; Swagger UI renders an array example as `[]` otherwise."""
    if depth > 6:  # a self-referential model would otherwise recurse forever
        return None
    while "$ref" in schema:
        schema = definitions.get(schema["$ref"].rsplit("/", 1)[-1], {})
    for combinator in ("anyOf", "oneOf", "allOf"):
        if combinator in schema:
            options = [option for option in schema[combinator] if option.get("type") != "null"]
            return _placeholder(options[0], definitions, depth + 1) if options else None
    if "enum" in schema:
        return schema["enum"][0]

    kind = schema.get("type")
    if kind == "object":
        return {
            name: _placeholder(field, definitions, depth + 1) for name, field in schema.get("properties", {}).items()
        }
    if kind == "array":
        return [_placeholder(schema.get("items", {}), definitions, depth + 1)]
    return {"integer": 0, "number": 0.0, "boolean": True, "null": None}.get(kind, "string")


class ListOf(RootModel[list[TItem]], Generic[TItem]):
    """
    A bare JSON array (unchanged on the wire) that gets a named schema component and a one-row example,
    which an inline array schema lacks in the OpenAPI renderers.
    """

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        schema = handler(core_schema)
        if "example" not in schema:
            item = cls.__pydantic_generic_metadata__.get("args", (None,))[0]
            if item is not None and hasattr(item, "model_json_schema"):
                item_schema = item.model_json_schema(ref_template="{model}")
                schema["example"] = [_placeholder(item_schema, item_schema.get("$defs", {}))]
        return schema


def resolve_shapes(default: ListShape | None, allowed: tuple[str, ...] | None, settings_default: str) -> tuple:
    """(default shape, shapes a client may ask for), the default always first. Raises `ShapeError`."""
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
    """The declared response model: one model for one shape, a union for several."""
    from .cursor import CursorPage
    from .list_query import PaginatedList

    models = {"plain": ListOf, "paginated": PaginatedList, "cursor": CursorPage}
    members = tuple(models[shape][item_type] for shape in shapes)
    # typing.Union, not `X | Y`: a types.UnionType cannot be re-subscripted with the item type.
    return members[0] if len(members) == 1 else Union[members]  # noqa: UP007


def shape_examples(shapes: tuple, item_type: Any) -> dict | None:
    """One response example per shape, named by its `X-List-Shape` value; None for a single shape."""
    if len(shapes) < 2:
        return None

    examples = {}
    for shape in shapes:
        model = response_model((shape,), item_type)
        schema = model.model_json_schema(ref_template="{model}")
        examples[shape] = {
            "summary": f"{SHAPE_HEADER}: {shape}",
            "value": _placeholder(schema, schema.get("$defs", {})),
        }
    return {"responses": {"200": {"content": {"application/json": {"examples": examples}}}}}
