"""
The built-in filters.

Each carries only its in-memory implementation. Backends register their own translations for these
same classes - see `backends/django_orm.py` - so adding an operator here does not require touching
any backend, and a backend that has not learned an operator yet simply declines the whole stage and
lets these run.

Comparisons guard against None rather than raising: a nullable column is normal, and a request that
asks for `year__gte=2000` is asking for records that have a year at least 2000, not for a crash on
the ones that have no year at all.
"""

from dataclasses import dataclass
from typing import Any, ClassVar

from .base import Filter

_REGISTERED: dict[str, type[Filter]] = {}


def register_operator(cls: type[Filter]) -> type[Filter]:
    """Makes an operator addressable by its lookup name in a filter declaration."""
    _REGISTERED[cls.lookup] = cls
    return cls


def operator_for(lookup: str) -> type[Filter]:
    try:
        return _REGISTERED[lookup]
    except KeyError:
        raise KeyError(f"unknown filter operator {lookup!r}; known: {', '.join(sorted(_REGISTERED))}") from None


def known_operators() -> frozenset[str]:
    return frozenset(_REGISTERED)


@register_operator
@dataclass(frozen=True)
class Exact(Filter):
    lookup: ClassVar[str] = "exact"

    def matches(self, record: Any) -> bool:
        return self.read(record) == self.value


@register_operator
@dataclass(frozen=True)
class IExact(Filter):
    lookup: ClassVar[str] = "iexact"

    def matches(self, record: Any) -> bool:
        actual = self.read(record)
        return actual is not None and str(actual).lower() == str(self.value).lower()


@register_operator
@dataclass(frozen=True)
class Contains(Filter):
    lookup: ClassVar[str] = "contains"

    def matches(self, record: Any) -> bool:
        actual = self.read(record)
        return actual is not None and str(self.value) in str(actual)


@register_operator
@dataclass(frozen=True)
class IContains(Filter):
    lookup: ClassVar[str] = "icontains"

    def matches(self, record: Any) -> bool:
        actual = self.read(record)
        return actual is not None and str(self.value).lower() in str(actual).lower()


@register_operator
@dataclass(frozen=True)
class StartsWith(Filter):
    lookup: ClassVar[str] = "startswith"

    def matches(self, record: Any) -> bool:
        actual = self.read(record)
        return actual is not None and str(actual).startswith(str(self.value))


@register_operator
@dataclass(frozen=True)
class Gt(Filter):
    lookup: ClassVar[str] = "gt"

    def matches(self, record: Any) -> bool:
        actual = self.read(record)
        return actual is not None and actual > self.value


@register_operator
@dataclass(frozen=True)
class Gte(Filter):
    lookup: ClassVar[str] = "gte"

    def matches(self, record: Any) -> bool:
        actual = self.read(record)
        return actual is not None and actual >= self.value


@register_operator
@dataclass(frozen=True)
class Lt(Filter):
    lookup: ClassVar[str] = "lt"

    def matches(self, record: Any) -> bool:
        actual = self.read(record)
        return actual is not None and actual < self.value


@register_operator
@dataclass(frozen=True)
class Lte(Filter):
    lookup: ClassVar[str] = "lte"

    def matches(self, record: Any) -> bool:
        actual = self.read(record)
        return actual is not None and actual <= self.value


@register_operator
@dataclass(frozen=True)
class In(Filter):
    """Value is one of several. The query parameter repeats: `?year__in=2001&year__in=2002`."""

    lookup: ClassVar[str] = "in"

    def matches(self, record: Any) -> bool:
        return self.read(record) in (self.value or ())


@register_operator
@dataclass(frozen=True)
class IsNull(Filter):
    lookup: ClassVar[str] = "isnull"

    def matches(self, record: Any) -> bool:
        return (self.read(record) is None) is bool(self.value)


@register_operator
@dataclass(frozen=True)
class Overlaps(Filter):
    """
    The record's own list shares at least one member with the requested one - for the
    `genres`/`moods` shape, where the field holds several values and any of them will do.
    """

    lookup: ClassVar[str] = "overlaps"

    def matches(self, record: Any) -> bool:
        actual = self.read(record)
        if actual is None or self.value is None:
            return False
        return bool(set(actual) & set(self.value))
