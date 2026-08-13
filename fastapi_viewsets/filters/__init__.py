"""
Declarative filters.

A viewset says which fields accept which operators, and gets query parameters, an OpenAPI schema
and in-memory filtering for free:

    TrackFilter = make_filter_model(Track, {
        "year": ["exact", "gte", "lte"],
        "title": ["icontains"],
        "genres": ["overlaps"],
    })

    class TrackViewSet(CollectionViewSet[int, Track], PaginatedListMixin[Track, TrackFilter]):
        ...

Each filter carries one implementation - `matches()`, in memory, always present. A backend that can
do better registers a compiler for the filters it understands (see registry.compiles); one it has
not learned simply leaves the whole stage to `matches()`. Hand-written `filter_list` keeps working
unchanged and is still the right answer for anything an operator would express badly.
"""

from .base import Filter, FilterError, FilterSet
from .declaration import FilterDeclaration, filters_from, make_filter_model, SPEC_ATTRIBUTE
from .operators import (
    Contains,
    Exact,
    Gt,
    Gte,
    IContains,
    IExact,
    In,
    IsNull,
    known_operators,
    Lt,
    Lte,
    operator_for,
    Overlaps,
    register_operator,
    StartsWith,
)
from .registry import can_compile_all, compile_all, compiler_for, compiles, registered_pairs

__all__ = [
    "SPEC_ATTRIBUTE",
    "Contains",
    "Exact",
    "Filter",
    "FilterDeclaration",
    "FilterError",
    "FilterSet",
    "Gt",
    "Gte",
    "IContains",
    "IExact",
    "In",
    "IsNull",
    "Lt",
    "Lte",
    "Overlaps",
    "StartsWith",
    "can_compile_all",
    "compile_all",
    "compiler_for",
    "compiles",
    "filters_from",
    "known_operators",
    "make_filter_model",
    "operator_for",
    "register_operator",
    "registered_pairs",
]
