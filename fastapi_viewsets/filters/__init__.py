"""
Declarative filters: `make_filter_model(Track, {"year": ["exact", "gte"]})` declares which fields
accept which operators and yields query parameters, an OpenAPI schema and in-memory filtering.

Each filter implements `matches()` in memory. A backend registers compilers for the filters it
understands (`registry.compiles`); a stage holding one it does not understand runs in memory.
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
