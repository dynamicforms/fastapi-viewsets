from typing import Annotated

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
