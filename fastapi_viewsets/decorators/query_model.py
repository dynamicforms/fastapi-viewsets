"""
A pydantic model read from the query string, one parameter per field. Used instead of `Depends(Model)`,
under which FastAPI reads a `list[...]` field from the body instead of a repeated query key.
"""

import inspect

from functools import cache
from typing import Annotated, Any

from fastapi import Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

_REQUEST_PARAMETER = "request"


def query_name(name: str, field: FieldInfo) -> str:
    """The query parameter name of a field: its string validation alias, else its alias, else its name."""
    if isinstance(field.validation_alias, str):
        return field.validation_alias
    return field.alias or name


def _query(field: FieldInfo, alias: str) -> Any:
    """The `Query()` of a field: its alias, title, description, examples and deprecation."""
    return Query(
        alias=alias,
        title=field.title,
        description=field.description,
        examples=field.examples,
        deprecated=field.deprecated,
        json_schema_extra=field.json_schema_extra if isinstance(field.json_schema_extra, dict) else None,
    )


def _parameter(index: int, name: str, field: FieldInfo) -> inspect.Parameter:
    """
    The dependency parameter of one field, named by position so it never collides with `request`.
    A `default_factory` field defaults to None here; the model applies its own default to an unsent field.
    """
    annotation = Annotated[(field.annotation, *field.metadata, _query(field, query_name(name, field)))]
    if field.is_required():
        default = inspect.Parameter.empty
    elif field.default_factory is not None:
        default = None
    else:
        default = field.default
    return inspect.Parameter(f"field_{index}", inspect.Parameter.KEYWORD_ONLY, annotation=annotation, default=default)


@cache
def query_model_dependency(model: type[BaseModel]):
    """
    A FastAPI dependency reading `model` from the query string; a sequence field from a repeated key.
    An unsent field takes the model's default; a model validator's error becomes a
    `RequestValidationError` at `("query", ...)`.
    """
    fields = list(model.model_fields.items())
    parameters = [inspect.Parameter(_REQUEST_PARAMETER, inspect.Parameter.KEYWORD_ONLY, annotation=Request)]
    parameters += [_parameter(index, name, field) for index, (name, field) in enumerate(fields)]
    names = [query_name(name, field) for name, field in fields]

    def read_query_model(**kwargs: Any) -> BaseModel:
        sent = kwargs[_REQUEST_PARAMETER].query_params
        data = {name: kwargs[f"field_{index}"] for index, name in enumerate(names) if name in sent}
        try:
            return model.model_validate(data)
        except ValidationError as error:
            raise RequestValidationError(
                [{**err, "loc": ("query", *err["loc"])} for err in error.errors(include_url=False)]
            ) from error

    read_query_model.__signature__ = inspect.Signature(parameters, return_annotation=model)
    read_query_model.__name__ = f"read_{model.__name__}"
    return read_query_model
