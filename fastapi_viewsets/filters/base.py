"""
Filters as data. A filter implements only `matches()`, the mandatory in-memory test, so a filter no
backend can translate still works. Backend translations are registered compilers (see registry.py),
so adding a filter or a backend needs no change to the other.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar


class FilterError(ValueError):
    """A filter was declared against something the model does not have."""


@dataclass(frozen=True)
class Filter(ABC):
    """One field, one operator, one value. Frozen: a backend that wants a variant builds a new one."""

    field: str
    value: Any

    lookup: ClassVar[str]
    """The suffix this filter answers to in a query parameter: `year__gte` selects `gte`."""

    @abstractmethod
    def matches(self, record: Any) -> bool:
        """Whether `record` passes. The universal implementation; never optional."""

    def fields(self) -> tuple[str, ...]:
        """Which model fields this filter reads, so a backend can check it addresses all of them."""
        return (self.field,)

    def read(self, record: Any) -> Any:
        """The field's value on a record: a dict key, else an attribute; None when absent."""
        if isinstance(record, dict):
            return record.get(self.field)
        return getattr(record, self.field, None)


class FilterSet:
    """
    The filters one request asked for, and the in-memory fallback for all of them. A backend compiles
    the whole set or none of it, as a partly compiled set would silently return too many rows.
    """

    __slots__ = ("filters",)

    def __init__(self, filters: list[Filter]):
        self.filters = filters

    def __bool__(self) -> bool:
        return bool(self.filters)

    def __iter__(self):
        return iter(self.filters)

    def __len__(self) -> int:
        return len(self.filters)

    def __repr__(self) -> str:
        inner = ", ".join(f"{f.field}__{f.lookup}={f.value!r}" for f in self.filters)
        return f"FilterSet({inner})"

    def matches(self, record: Any) -> bool:
        return all(fltr.matches(record) for fltr in self.filters)

    def apply(self, records: list) -> list:
        return [record for record in records if self.matches(record)]
