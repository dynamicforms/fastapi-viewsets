"""
Turning a filter declaration into query parameters, and query parameters back into filters.

A viewset declares which fields accept which operators:

    TrackFilter = make_filter_model(Track, {"year": ["exact", "gte", "lte"], "title": ["icontains"]})

which becomes the query parameters `year`, `year__gte`, `year__lte`, `title__icontains` - and
nothing else. Generating every field crossed with every operator would put dozens of parameters in
the OpenAPI schema, most of them meaningless for the field they hang off, so the declaration is
explicit. `exact` keeps the bare field name, since `?year=2003` is what anyone would write.

The resulting model is an ordinary pydantic model, so FastAPI validates and documents the
parameters exactly as it did for `make_all_optional` - which is now the special case of "every
field, exact only".
"""

from typing import Any, get_args, get_origin, Union

from pydantic import BaseModel, create_model, Field, TypeAdapter

from .base import Filter, FilterError, FilterSet
from .operators import known_operators, operator_for

FilterDeclaration = dict[str, list[str]]

SPEC_ATTRIBUTE = "__filter_spec__"
"""Where the (parameter name -> field, operator, item type) map is stashed on a generated model."""

_LIST_LOOKUPS = frozenset({"in", "overlaps"})
"""Operators whose value is several values, sent comma-separated. See _parameter_annotation."""


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
    Always a scalar, even for the operators that mean several values.

    FastAPI cannot expose a list-typed field of a `Depends()`-expanded model as a query parameter:
    it drops the field from the schema and never populates it, with or without an explicit
    `Query()` annotation. Since that expansion is how `route_viewset` turns a filter model into
    individual query parameters, `in` and `overlaps` take a comma-separated string instead - the
    same convention the `sort` parameter already uses.
    """
    if lookup == "isnull":
        return bool | None
    if lookup in _LIST_LOOKUPS:
        return str | None
    return _optional(field_annotation)


def make_filter_model(model: type[BaseModel], declaration: FilterDeclaration) -> type[BaseModel]:
    """
    Builds the query-parameter model for a filter declaration.

    Raises rather than ignoring an unknown field or operator: a typo in a declaration would
    otherwise produce a filter parameter that silently never matches anything, which is the kind of
    bug that gets found in production by someone wondering why their search box does nothing.
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
            default = Field(None, description="Comma-separated list of values") if lookup in _LIST_LOOKUPS else None
            fields[parameter] = (annotation, default)
            spec[parameter] = (field_name, lookup, _item_annotation(field_annotation, lookup))

    filter_model = create_model(f"{model.__name__}Filter", **fields)
    setattr(filter_model, SPEC_ATTRIBUTE, spec)
    return filter_model


def filters_from(fltr: Any) -> FilterSet | None:
    """
    The FilterSet a populated filter model asks for, or None when the model was not built from a
    declaration - in which case the viewset is on the older hand-written `filter_list` path and
    this machinery stays out of its way.
    """
    spec = getattr(type(fltr), SPEC_ATTRIBUTE, None)
    if spec is None:
        return None

    filters: list[Filter] = []
    for parameter, (field_name, lookup, item_annotation) in spec.items():
        value = getattr(fltr, parameter, None)
        if value is None:
            continue
        if lookup in _LIST_LOOKUPS:
            value = _split_values(value, item_annotation)
            if not value:
                continue
        filters.append(operator_for(lookup)(field=field_name, value=value))
    return FilterSet(filters)


def _split_values(raw: Any, item_annotation: Any) -> list:
    """
    Comma-separated string to a validated list of the field's own type.

    Coercion matters: `?year__in=1957,1959` arrives as strings, and comparing those to an integer
    column would quietly match nothing. An entry that will not convert is refused rather than
    dropped - a filter that silently ignores half of what it was given is worse than one that says
    it could not.
    """
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = [part.strip() for part in str(raw).split(",") if part.strip()]
    if not items:
        return []
    try:
        return TypeAdapter(list[item_annotation]).validate_python(items)
    except Exception as error:
        raise FilterError(f"cannot read {raw!r} as a list of {item_annotation}: {error}") from None
