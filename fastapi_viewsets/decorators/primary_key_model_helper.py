import copy
import inspect
import types

from typing import Annotated, Any, get_args, get_origin, TypeVar, Union

from pydantic import BaseModel, create_model, field_validator
from pydantic.fields import FieldInfo

T = TypeVar("T", bound=BaseModel)


def create_model_without_pk(model: type[T], pk_field_name: str) -> type[BaseModel]:
    """
    Creates a new Pydantic model based on the given model but without the specified primary key field.
    """
    fields = {
        name: (field.annotation, field.default if field.default is not ... else ...)
        for name, field in model.model_fields.items()
        if name != pk_field_name
    }
    return create_model(f"{model.__name__}NoPK", **fields)


def typecast_to_original_model(value: Any, original_annotation: type[T]) -> T:
    """
    Typecasts a value (usually a Pydantic model without PK) back to the original Pydantic model.
    If validation fails (e.g., due to missing PK that will be auto-incremented), it uses model_construct.
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

    if not isinstance(value, BaseModel) or isinstance(value, origin):
        return value

    # noinspection PyBroadException
    try:
        return annotation.model_validate(value.model_dump())
    except Exception:
        # Fallback: create instance with model_construct if validation fails
        # (e.g. required field missing but it will be filled by autoinc)
        return annotation.model_construct(**value.model_dump())


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
