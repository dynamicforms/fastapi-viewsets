"""
Filters as data.

A filter carries exactly one implementation of its own: `matches()`, which decides whether a single
record passes, in memory. That one is mandatory, and it is what makes "the backend could not
translate this" a non-event rather than an error - there is always something able to answer.

Every *other* implementation lives with the backend that provides it, registered rather than
inherited (see registry.py). That asymmetry is the whole design: if a filter carried an
implementation per backend, adding a backend would mean editing every filter, and adding a filter
would mean editing every backend. Registered compilers close neither door.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar


class FilterError(ValueError):
    """A filter was declared against something the model does not have."""


@dataclass(frozen=True)
class Filter(ABC):
    """
    One field, one operator, one value.

    Frozen because a filter describes a request and nothing should be editing it halfway through a
    pipeline - a backend that wants a variant builds a new one.
    """

    field: str
    value: Any

    lookup: ClassVar[str]
    """The suffix this filter answers to in a query parameter: `year__gte` selects `gte`."""

    @abstractmethod
    def matches(self, record: Any) -> bool:
        """Whether `record` passes. The universal implementation; never optional."""

    def read(self, record: Any) -> Any:
        """
        The field's value on a record, whatever the record is.

        Pydantic models, ORM instances and plain objects all answer to getattr; a mapping does not,
        and viewsets backed by dicts of dicts are common enough to be worth the two lines.
        """
        if isinstance(record, dict):
            return record.get(self.field)
        return getattr(record, self.field, None)


class FilterSet:
    """
    The filters one request asked for, and the in-memory fallback for all of them.

    Deliberately all-or-nothing: `matches` applies every filter, and a backend either compiles the
    whole set or leaves the whole set alone. Partial push-down would mean a filter half-answered in
    SQL and half-forgotten, which returns too many rows and looks like it worked.
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
