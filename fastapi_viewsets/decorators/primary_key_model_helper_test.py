from typing import Annotated, Optional, Union

from pydantic import BaseModel

from fastapi_viewsets.decorators.primary_key_model_helper import (
    create_model_without_pk,
    typecast_to_original_model,
)


class Item(BaseModel):
    id: int
    name: str


def test_create_model_without_pk_drops_the_pk_field():
    without_pk = create_model_without_pk(Item, "id")
    assert set(without_pk.model_fields) == {"name"}


def test_typecast_returns_value_unchanged_when_already_the_target_model():
    item = Item(id=1, name="test")
    assert typecast_to_original_model(item, Item) is item


def test_typecast_falls_back_to_model_construct_when_a_required_field_is_missing():
    without_pk = create_model_without_pk(Item, "id")
    value = without_pk(name="test")
    result = typecast_to_original_model(value, Item)
    assert isinstance(result, Item)
    assert result.name == "test"


def test_typecast_unwraps_an_annotated_original_annotation():
    """`typecast_to_original_model` sees `Annotated[Item, ...]` when a route param carries FastAPI
    metadata (e.g. `Annotated[T, Query(...)]`) alongside its TypeVar. `get_origin` on that returns
    `Annotated` itself, not `Item` - passing it straight to `isinstance` raises `TypeError:
    typing.Annotated cannot be used with isinstance()` on Python 3.13+."""
    item = Item(id=1, name="test")
    assert typecast_to_original_model(item, Annotated[Item, "some fastapi metadata"]) is item


def test_typecast_unwraps_a_pep604_union_original_annotation():
    """`get_origin(Item | None)` returns `types.UnionType`, which no value is ever an instance of -
    passing it straight to `isinstance` always misses, sending an already-correct value into the
    `model_validate`/`model_construct` fallback below where `original_annotation` (the union object
    itself) has neither method."""
    item = Item(id=1, name="test")
    assert typecast_to_original_model(item, Item | None) is item
    assert typecast_to_original_model(None, Item | None) is None


def test_typecast_unwraps_a_typing_union_original_annotation():
    """`typing.Union`/`Optional` (as opposed to PEP 604's `Item | None`) is its own `get_origin()`
    result and needs the same unwrapping."""
    item = Item(id=1, name="test")
    assert typecast_to_original_model(item, Optional[Item]) is item  # noqa: UP045
    assert typecast_to_original_model(item, Union[Item, None]) is item  # noqa: UP007


def test_typecast_falls_back_to_model_construct_for_a_union_annotation():
    without_pk = create_model_without_pk(Item, "id")
    value = without_pk(name="test")
    result = typecast_to_original_model(value, Item | None)
    assert isinstance(result, Item)
    assert result.name == "test"
