"""
Which backend can translate which filter.

The lookup is keyed on both, because neither alone decides it: a filter has no idea how to become
SQL, and a backend has no idea what operators exist. Registering the pair outside both is what lets
a new operator arrive without a backend release, and a new backend without touching a single filter.

Registration is inheritance-aware in both directions - a viewset subclassing `DjangoORMViewSet`
inherits its compilers, and a compiler registered for a filter base class serves its subclasses -
so the common case needs no registration at all.
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
    The most specific registered compiler, or None.

    Walks the backend's MRO outward, and for each backend the filter's MRO outward, so the most
    specific backend wins over a more specific filter - a viewset that registered its own handling
    of a whole filter family should not be overruled by its base class's handling of one member.
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
    """
    Whether every filter in the set has a compiler.

    All or nothing on purpose: pushing some filters into the backend and forgetting the rest
    returns too many rows while looking like it worked. The caller either compiles the lot or
    leaves the lot to the in-memory pass.
    """
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
