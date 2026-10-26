"""
A pydantic model read from the query string, one query parameter per field.

`route_viewset` reads the filter model of a list or lookup endpoint through `query_model_dependency`
rather than through `Depends(Model)`: FastAPI reads a sequence-typed field (`list[int]`) of a model
expanded by `Depends(Model)` from the request body, so `?ids=1&ids=2` never reaches the model and the
schema documents the field as a body.
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
    """The query parameter a model field is read from: its validation alias when it is a string, its
    alias, otherwise its name."""
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
    The dependency parameter of one model field: `Annotated[<annotation>, *<constraints>, Query()]`.

    The parameter is named by position, so no field name collides with `request`; the field's query
    name is the `Query()` alias. A required field is a required parameter. An optional field's
    parameter default is the field's default, or None for a `default_factory` - the model applies its
    own default to a parameter the request does not send (see `query_model_dependency`).
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
    A FastAPI dependency that reads `model` from the query string.

    Every field of the model is a query parameter of the endpoint, documented in the schema with its
    type, default, description and constraints. A sequence-typed field is read from a repeated key
    (`?ids=1&ids=2`). The parameters the request sends are validated into `model`; a field the
    request does not send takes the model's default. A validation error of the model itself (a model
    validator) is a `RequestValidationError` with `loc` `("query", ...)`, as a parameter's own is.
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
