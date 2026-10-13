import copy
import inspect
import types

from typing import Annotated, Any, get_args, get_origin, TypeVar, Union

from pydantic import AliasChoices, BaseModel, create_model, Field, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic.json_schema import SkipJsonSchema
from pydantic_core import core_schema, PydanticCustomError

T = TypeVar("T", bound=BaseModel)


class _AssignedByServer:
    """The type of the primary key in `create_model_without_pk`: every value given for it fails
    validation with `pk_assigned_by_server`."""

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        def reject(_value):
            raise PydanticCustomError("pk_assigned_by_server", "The primary key is assigned by the server")

        return core_schema.no_info_plain_validator_function(reject)


def create_model_without_pk(model: type[T], pk_field_name: str) -> type[BaseModel]:
    """
    The body of a create of `model`: a subclass of `model` whose `pk_field_name` is not in the JSON
    schema and is None. The body may leave the primary key out or give it as null; any other value
    fails validation with `pk_assigned_by_server`. Every other field, the model's config and its
    validators are inherited. A `@field_validator` of the primary key in `after` mode never runs; one
    in `before`, `wrap` or `plain` mode runs on a value other than null. A `@model_validator` in
    `after` mode sees the primary key as None.
    """
    original = model.model_fields[pk_field_name]
    pk = Field(default=None, validate_default=False, alias=original.alias, validation_alias=original.validation_alias)
    aliases = original.validation_alias.choices if isinstance(original.validation_alias, AliasChoices) else []
    keys = {key for key in (pk_field_name, original.alias, original.validation_alias, *aliases) if isinstance(key, str)}

    def drop_null_pk(_cls, data):
        if isinstance(data, dict) and any(key in data and data[key] is None for key in keys):
            data = {key: value for key, value in data.items() if key not in keys or value is not None}
        return data

    return create_model(
        f"{model.__name__}NoPK",
        __base__=model,
        __validators__={"drop_null_pk": model_validator(mode="before")(classmethod(drop_null_pk))},
        **{pk_field_name: (SkipJsonSchema[_AssignedByServer], pk)},
    )


def _is_model_without_pk(model: type[BaseModel]) -> bool:
    return any(field.annotation is _AssignedByServer for field in model.model_fields.values())


def _without_pk_to_original_model(value: BaseModel, model: type[T]) -> T:
    """`value`, an instance of `create_model_without_pk(model, ...)`, as a `model` without the primary
    key unless `model` gives it a default. Its fields are taken as validated, not validated again."""
    data = {
        name: getattr(value, name)
        for name, field in type(value).model_fields.items()
        if field.annotation is not _AssignedByServer
    }
    return model.model_construct(_fields_set=set(value.model_fields_set), **data)


def typecast_to_original_model(value: Any, original_annotation: type[T]) -> T:
    """
    `value` as `original_annotation`, the parameter's own annotation. An instance of
    `create_model_without_pk(model, ...)` becomes a `model` (see `_without_pk_to_original_model`);
    an instance of the model itself and a value that is no model are returned as they are; any other
    model is validated as the model.
    """
    annotation = original_annotation
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]

    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        # Optional[Model] / Model | None: unwrap to the model member so isinstance() and
        # model_validate()/model_construct() below run against the model class rather than
        # the union object itself (which has neither).
        annotation = next(
            (arg for arg in get_args(annotation) if isinstance(arg, type) and issubclass(arg, BaseModel)),
            annotation,
        )
        origin = get_origin(annotation) or annotation
    else:
        origin = origin or annotation

    if not isinstance(value, BaseModel):
        return value
    if isinstance(value, origin) and _is_model_without_pk(type(value)):
        return _without_pk_to_original_model(value, annotation)
    if isinstance(value, origin):
        return value
    return annotation.model_validate(value.model_dump())


def create_partial_model(model: type[T], pk_field_name: str | None) -> type[BaseModel]:
    """
    The body of a partial update of `model`: every field optional, without `pk_field_name`. A field
    the body states is validated by its annotation, its constraints and the model's
    `@field_validator`s; one it leaves out stays unset. `@model_validator`s are not applied: they see
    the whole model, and a partial body is not one.
    """
    fields = {
        name: (field.annotation, _without_default(field))
        for name, field in model.model_fields.items()
        if name != pk_field_name
    }
    validators = {
        name: field_validator(*decorator.info.fields, mode=decorator.info.mode, check_fields=False)(
            getattr(decorator.func, "__func__", decorator.func)
        )
        for name, decorator in model.__pydantic_decorators__.field_validators.items()
    }
    return create_model(f"{model.__name__}Partial", __config__=model.model_config, __validators__=validators, **fields)


def _without_default(field: FieldInfo) -> FieldInfo:
    """`field` with None for its default, which is never validated: an omitted field stays unset."""
    field = copy.copy(field)
    field.default = None
    field.default_factory = None
    field.validate_default = None
    return field


def partial_to_original_model(value: BaseModel, model: type[T]) -> T:
    """
    `value`, an instance of `create_partial_model(model, ...)`, as a `model` that holds the fields
    `value` set, as validated, and no others: its `model_fields_set` names them, so
    `model_dump(exclude_unset=True)` returns exactly what the request stated.
    """
    data = {name: getattr(value, name) for name in value.model_fields_set}
    return model.model_construct(_fields_set=set(data), **data)


PARTIAL_UPDATE_ACTIONS = frozenset({"partial_update", "bulk_partial_update"})
"""The mixin endpoints whose body is a partial update: `PATCH {pk}` and `PATCH bulk`."""


def partial_body_annotation(annotation, pk_field_name: str | None):
    """The body annotation of a partial update in place of `annotation`, a model or a `dict` of
    models keyed by pk; None for any other annotation."""
    if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
        return create_partial_model(annotation, pk_field_name)
    if get_origin(annotation) is dict:
        key, model = get_args(annotation)
        if inspect.isclass(model) and issubclass(model, BaseModel):
            return dict[key, create_partial_model(model, pk_field_name)]
    return None


def from_partial_body(value, annotation):
    """A validated partial-update body as `annotation`, the parameter's own: each model holds the
    fields the request stated and no others."""
    if get_origin(annotation) is dict:
        model = get_args(annotation)[1]
        return {key: partial_to_original_model(item, model) for key, item in value.items()}
    return partial_to_original_model(value, annotation)
