from typing import Any, get_origin, TypeVar

from pydantic import BaseModel, create_model

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
    origin = get_origin(original_annotation) or original_annotation
    if not isinstance(value, BaseModel) or isinstance(value, origin):
        return value

    # noinspection PyBroadException
    try:
        return original_annotation.model_validate(value.model_dump())
    except Exception:
        # Fallback: create instance with model_construct if validation fails
        # (e.g. required field missing but it will be filled by autoinc)
        return original_annotation.model_construct(**value.model_dump())
