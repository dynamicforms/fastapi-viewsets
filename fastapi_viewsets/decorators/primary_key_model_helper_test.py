from typing import Annotated, Optional, Union

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator, ValidationError

from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.decorators.primary_key_model_helper import (
    create_model_without_pk,
    typecast_to_original_model,
)
from fastapi_viewsets.mixins import CreateMixin


class Item(BaseModel):
    id: int
    name: str


class Product(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str = Field(min_length=3, alias="title")
    price: int = Field(ge=0, description="Price in cents")

    @field_validator("id")
    @classmethod
    def positive_id(cls, value):
        if value <= 0:
            raise ValueError("id must be positive")
        return value

    @field_validator("name")
    @classmethod
    def suffixed_name(cls, value):
        return f"{value}!"


def errors(model: type[BaseModel], data: dict) -> list[tuple]:
    with pytest.raises(ValidationError) as caught:
        model.model_validate(data)
    return sorted((error["loc"], error["type"]) for error in caught.value.errors())


def test_create_model_without_pk_leaves_the_pk_out_of_the_json_schema():
    schema = create_model_without_pk(Product, "id").model_json_schema()
    assert set(schema["properties"]) == {"title", "price"}
    assert schema["required"] == ["title", "price"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["title"]["minLength"] == 3
    assert schema["properties"]["price"]["minimum"] == 0
    assert schema["properties"]["price"]["description"] == "Price in cents"


def test_create_model_without_pk_applies_the_field_constraints_and_the_model_config():
    without_pk = create_model_without_pk(Product, "id")
    assert errors(without_pk, {"title": "", "price": -10, "extra": 1}) == [
        (("extra",), "extra_forbidden"),
        (("price",), "greater_than_equal"),
        (("title",), "string_too_short"),
    ]


@pytest.mark.parametrize("pk", [5, 0, "x", ""])
def test_create_model_without_pk_rejects_a_pk_other_than_null(pk):
    without_pk = create_model_without_pk(Product, "id")
    assert errors(without_pk, {"id": pk, "title": "abc", "price": 1}) == [(("id",), "pk_assigned_by_server")]


def test_create_model_without_pk_accepts_a_null_pk_without_running_its_validators():
    value = create_model_without_pk(Product, "id").model_validate({"id": None, "title": "abc", "price": 1})
    assert value.id is None
    assert value.model_fields_set == {"name", "price"}


def test_create_model_without_pk_reads_the_pk_by_its_alias():
    class Aliased(BaseModel):
        model_config = ConfigDict(extra="forbid")
        id: int = Field(alias="pk")
        name: str

    without_pk = create_model_without_pk(Aliased, "id")
    assert without_pk.model_validate({"pk": None, "name": "a"}).name == "a"
    assert errors(without_pk, {"pk": 5, "name": "a"}) == [(("pk",), "pk_assigned_by_server")]


def test_create_model_without_pk_accepts_alias_choices_on_the_pk():
    class Choices(BaseModel):
        id: int = Field(validation_alias=AliasChoices("id", "pk"))
        name: str

    without_pk = create_model_without_pk(Choices, "id")
    assert without_pk.model_validate({"id": None, "name": "a"}).name == "a"
    assert without_pk.model_validate({"pk": None, "name": "a"}).name == "a"
    assert errors(without_pk, {"pk": 5, "name": "a"}) == [(("pk",), "pk_assigned_by_server")]


def test_create_model_without_pk_rejects_a_pk_with_a_default():
    class Defaulted(BaseModel):
        id: int | None = None
        name: str

    without_pk = create_model_without_pk(Defaulted, "id")
    assert errors(without_pk, {"id": 5, "name": "a"}) == [(("id",), "pk_assigned_by_server")]


def test_create_model_without_pk_runs_each_field_validator_once():
    value = create_model_without_pk(Product, "id").model_validate({"title": "abc", "price": 1})
    result = typecast_to_original_model(value, Product)
    assert type(result) is Product
    assert result.name == "abc!"


def test_create_model_without_pk_keeps_a_validate_default_config():
    class Strict(BaseModel):
        model_config = ConfigDict(validate_default=True)
        id: int
        name: str = "a"

    value = create_model_without_pk(Strict, "id").model_validate({})
    assert value.name == "a"


def test_a_model_validator_sees_the_pk_as_none():
    seen = []

    class Checked(BaseModel):
        id: int
        name: str

        @model_validator(mode="after")
        def record(self):
            seen.append(self.id)
            return self

    create_model_without_pk(Checked, "id").model_validate({"name": "a"})
    assert seen == [None]


def test_typecast_returns_value_unchanged_when_already_the_target_model():
    item = Item(id=1, name="test")
    assert typecast_to_original_model(item, Item) is item


def test_typecast_builds_the_original_model_without_the_pk():
    without_pk = create_model_without_pk(Item, "id")
    value = without_pk(name="test")
    result = typecast_to_original_model(value, Item)
    assert type(result) is Item
    assert result.name == "test"
    assert result.model_fields_set == {"name"}
    assert "id" not in result.__dict__


def test_typecast_gives_a_defaulted_pk_its_default():
    class Defaulted(BaseModel):
        id: int | None = None
        name: str
        tags: list[str] = Field(default_factory=list)

    value = create_model_without_pk(Defaulted, "id").model_validate({"name": "a"})
    result = typecast_to_original_model(value, Defaulted)
    assert type(result) is Defaulted
    assert result.model_dump() == {"id": None, "name": "a", "tags": []}
    assert result.model_fields_set == {"name"}


def test_typecast_keeps_a_nested_model_as_the_model():
    class Owner(BaseModel):
        name: str

    class Pet(BaseModel):
        id: int
        owner: Owner

    value = create_model_without_pk(Pet, "id").model_validate({"owner": {"name": "a"}})
    assert typecast_to_original_model(value, Pet).owner == Owner(name="a")


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


def test_typecast_builds_the_original_model_for_a_union_annotation():
    without_pk = create_model_without_pk(Item, "id")
    value = without_pk(name="test")
    result = typecast_to_original_model(value, Item | None)
    assert type(result) is Item
    assert result.name == "test"


def make_product_app(created: list) -> FastAPI:
    router = APIRouter()

    @route_viewset(router=router, base_path="/products", pk_field_name="id")
    class ProductViewSet(CreateMixin[int, Product]):
        async def perform_create(self, _context, data: Product) -> Product:
            created.append(data)
            return Product.model_validate({"id": 1, "title": data.name, "price": data.price})

    app = FastAPI()
    app.include_router(router)
    return app


def test_a_create_validates_the_body_by_the_model():
    created = []
    client = TestClient(make_product_app(created))

    response = client.post("/products", json={"title": "", "price": -10})
    assert response.status_code == 422
    fields = response.json()["field_errors"]
    assert {error["code"] for errors in fields.values() for error in errors} == {
        "pydantic.string_too_short",
        "pydantic.greater_than_equal",
    }

    response = client.post("/products", json={"id": 7, "title": "abc", "price": 1})
    assert response.status_code == 422
    assert [error["code"] for errors in response.json()["field_errors"].values() for error in errors] == [
        "fastapi-viewsets.pk_assigned_by_server"
    ]

    assert created == []


def test_a_create_hands_perform_create_the_validated_model():
    created = []
    client = TestClient(make_product_app(created))

    response = client.post("/products", json={"id": None, "title": "abc", "price": 1})
    assert response.status_code == 200
    assert type(created[0]) is Product
    assert created[0].name == "abc!"
