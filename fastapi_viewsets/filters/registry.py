"""
Which backend can translate which filter: compilers registered per (backend, filter type) pair.
Lookup follows inheritance on both sides, so a `DjangoORMViewSet` subclass inherits its compilers and
a compiler for a filter base class serves its subclasses.
"""

from collections.abc import Callable
from typing import Any, TypeVar

from .base import Filter

Compiler = Callable[[Any, Filter, Any], Any]
"""(viewset, filter, query) -> query. Returns the narrowed query, whatever the backend's query is."""

F = TypeVar("F", bound=type[Filter])

_compilers: dict[tuple[type, type[Filter]], Compiler] = {}


def compiles(backend: type, filter_type: type[Filter]) -> Callable[[Compiler], Compiler]:
    """
    Registers how `backend` translates `filter_type` into its own query language.

        @compiles(DjangoORMViewSet, Exact)
        def _(viewset, fltr, queryset):
            return queryset.filter(**{fltr.field: fltr.value})
    """

    def decorator(compiler: Compiler) -> Compiler:
        _compilers[(backend, filter_type)] = compiler
        return compiler

    return decorator


def compiler_for(backend: type, filter_type: type[Filter]) -> Compiler | None:
    """
    The most specific registered compiler, or None. The backend's MRO is the outer loop, so a more
    specific backend wins over a more specific filter.
    """
    for backend_class in backend.__mro__:
        for filter_class in filter_type.__mro__:
            if not isinstance(filter_class, type) or not issubclass(filter_class, Filter):
                continue
            compiler = _compilers.get((backend_class, filter_class))
            if compiler is not None:
                return compiler
    return None


def can_compile_all(backend: type, filters) -> bool:
    """Whether every filter in the set has a compiler; the caller compiles all of them or none."""
    return all(compiler_for(backend, type(fltr)) is not None for fltr in filters)


def compile_all(backend: type, viewset: Any, filters, query: Any) -> Any:
    """Applies every filter's compiler in turn. Only valid when `can_compile_all` said yes."""
    for fltr in filters:
        compiler = compiler_for(backend, type(fltr))
        if compiler is None:  # pragma: no cover - guarded by can_compile_all
            raise LookupError(f"no compiler for {type(fltr).__name__} on {backend.__name__}")
        query = compiler(viewset, fltr, query)
    return query


def registered_pairs() -> list[tuple[str, str]]:
    """Every (backend, filter) pair currently registered - for diagnostics and tests."""
    return sorted((backend.__name__, filter_type.__name__) for backend, filter_type in _compilers)
