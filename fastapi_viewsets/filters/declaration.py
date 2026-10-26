"""
Turning a filter declaration into query parameters, and query parameters back into filters.

A viewset declares which fields accept which operators:

    TrackFilter = make_filter_model(Track, {"year": ["exact", "gte", "lte"], "title": ["icontains"]})

which becomes the query parameters `year`, `year__gte`, `year__lte`, `title__icontains` - and
nothing else. Generating every field crossed with every operator would put dozens of parameters in
the OpenAPI schema, most of them meaningless for the field they hang off, so the declaration is
explicit. `exact` keeps the bare field name, since `?year=2003` is what anyone would write.

The resulting model is an ordinary pydantic model, so FastAPI validates and documents the
parameters as it does for `make_all_optional`, which is the case of "every field, exact only".
"""

from typing import Any, get_args, get_origin, Union

from pydantic import BaseModel, create_model, Field

from .base import Filter, FilterError, FilterSet
from .operators import known_operators, operator_for

FilterDeclaration = dict[str, list[str]]

SPEC_ATTRIBUTE = "__filter_spec__"
"""Where the (parameter name -> field, operator, item type) map is stashed on a generated model."""

_LIST_LOOKUPS = frozenset({"in", "overlaps"})
"""Operators whose value is several values, read from a repeated query parameter."""

_LIST_DESCRIPTION = "One or more values; the parameter repeats for each (`?{parameter}=a&{parameter}=b`)."


def _optional(annotation: Any) -> Any:
    if get_origin(annotation) is Union and type(None) in get_args(annotation):
        return annotation
    return annotation | None


def _item_annotation(field_annotation: Any, lookup: str) -> Any:
    """The type of a single value for this operator, before any list wrapping."""
    if lookup == "isnull":
        return bool
    if lookup == "overlaps":
        inner = get_args(field_annotation)
        return inner[0] if inner else field_annotation
    return field_annotation


def _parameter_annotation(field_annotation: Any, lookup: str) -> Any:
    """
    The type of the query parameter: a list of the item type for `in` and `overlaps`, read from a
    repeated key (`?year__in=1999&year__in=2000`) and validated item by item into the field's own
    type; a scalar for every other operator.
    """
    if lookup == "isnull":
        return bool | None
    if lookup in _LIST_LOOKUPS:
        return list[_item_annotation(field_annotation, lookup)] | None
    return _optional(field_annotation)


def make_filter_model(model: type[BaseModel], declaration: FilterDeclaration) -> type[BaseModel]:
    """
    Builds the query-parameter model for a filter declaration.

    Raises rather than ignoring an unknown field or operator: a typo in a declaration would
    otherwise produce a filter parameter that silently never matches anything.
    """
    fields: dict[str, tuple[Any, Any]] = {}
    spec: dict[str, tuple[str, str, Any]] = {}

    for field_name, lookups in declaration.items():
        if field_name not in model.model_fields:
            raise FilterError(
                f"{model.__name__} has no field {field_name!r} to filter on; "
                f"known: {', '.join(sorted(model.model_fields))}"
            )
        field_annotation = model.model_fields[field_name].annotation

        for lookup in lookups:
            if lookup not in known_operators():
                raise FilterError(
                    f"unknown filter operator {lookup!r} on {field_name!r}; "
                    f"known: {', '.join(sorted(known_operators()))}"
                )
            parameter = field_name if lookup == "exact" else f"{field_name}__{lookup}"
            annotation = _parameter_annotation(field_annotation, lookup)
            default = (
                Field(None, description=_LIST_DESCRIPTION.format(parameter=parameter))
                if lookup in _LIST_LOOKUPS
                else None
            )
            fields[parameter] = (annotation, default)
            spec[parameter] = (field_name, lookup, _item_annotation(field_annotation, lookup))

    filter_model = create_model(f"{model.__name__}Filter", **fields)
    setattr(filter_model, SPEC_ATTRIBUTE, spec)
    return filter_model


def filters_from(fltr: Any) -> FilterSet | None:
    """
    The FilterSet a populated filter model asks for, or None when the model was not built from a
    declaration - in which case the viewset is on the hand-written `filter_list` path and
    this machinery stays out of its way.
    """
    spec = getattr(type(fltr), SPEC_ATTRIBUTE, None)
    if spec is None:
        return None

    filters: list[Filter] = []
    for parameter, (field_name, lookup, _item) in spec.items():
        value = getattr(fltr, parameter, None)
        if value is None or (lookup in _LIST_LOOKUPS and not value):
            continue
        filters.append(operator_for(lookup)(field=field_name, value=value))
    return FilterSet(filters)
