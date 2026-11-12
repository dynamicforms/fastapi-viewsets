"""
The list request (`ListQuery`) and answer types. `perform_list` returns `ListRecords`; `apply_filter`,
`apply_sort`, `take_page` and `count_records` receive the `ListQuery`, so a backend pushes part of the
query down by overriding one of them. See docs/guide/list-pipeline.md.
"""

import inspect

from collections.abc import AsyncIterable, AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

TItem = TypeVar("TItem")
"""
Not the mixins' TypeVar: Pydantic returns the bare class for `M[X]` when X is M's own parameter, which
would strip the item type from `PaginatedList[T]`.
"""

ListRecords = Iterable[TItem] | AsyncIterable[TItem]
"""Any sync or async iterable flowing through the list pipeline; `to_record` converts its items."""


@dataclass
class ListQuery:
    """
    The list request. A backend that performed a stage in its store calls `mark_applied(stage)`, and
    the in-memory step for that stage is skipped.
    """

    fltr: Any = None
    """The filter model instance; None when the request filters nothing."""
    sort: list = field(default_factory=list)
    """The requested order (`SortState`); empty for no order."""
    offset: int = 0
    """Number of records to skip."""
    limit: int | None = None
    """Page size; None when the request sent no `limit`."""
    applied: set[str] = field(default_factory=set)
    """Stages a backend performed in its store: `filter`, `sort`, `pagination`, `conversion`."""
    cursor: str | None = None
    """The cursor the request sent, for the cursor shape."""
    shape: str | None = None
    """The answer envelope; when unset, `get_list` uses the viewset's default shape."""
    extra_filters: list = field(default_factory=list)
    """Filters the pipeline adds itself (the cursor predicate), applied in the filter stage."""

    @property
    def has_filter(self) -> bool:
        """Whether the request filters: a filter model or a pipeline filter."""
        return self.fltr is not None or bool(self.extra_filters)

    @property
    def has_sort(self) -> bool:
        """Whether the request sets an order."""
        return bool(self.sort)

    @property
    def is_paginated(self) -> bool:
        """Whether the request sent a `limit`."""
        return self.limit is not None

    def mark_applied(self, *stages: str) -> None:
        """Records `stages` as performed, so their in-memory steps are skipped."""
        self.applied.update(stages)

    def needs(self, stage: str) -> bool:
        """Whether the in-memory step of `stage` still has to run."""
        return stage not in self.applied


_PAGINATED_LIST_DESCRIPTION = (
    "One page of results; `count` is null when the source cannot be counted without draining it."
)


class PaginatedList(BaseModel, Generic[TItem]):
    """One page of results; `count` is null when the source cannot be counted without draining it."""

    # A parametrised model (`PaginatedList[Track]`) has no docstring of its own; the config carries the description.
    model_config = ConfigDict(
        use_attribute_docstrings=True, json_schema_extra={"description": inspect.cleandoc(__doc__)}
    )

    results: list[TItem]
    """The records of the page."""
    offset: int
    """Number of records before the page."""
    limit: int | None
    """Page size the page was read with."""
    count: int | None = None
    """Number of records in the whole list; null when the source cannot be counted."""
    has_more: bool = False
    """Whether records follow the page."""
    has_previous: bool = False
    """Whether records precede the page."""


def build_list_query(fltr: Any, sort: str | None, offset: int = 0, limit: int | None = None) -> ListQuery:
    """Build a ListQuery from raw query parameters; a filter model with every field None counts as no filter."""
    from .mixins import parse_sort_param

    has_filter = (
        fltr is not None
        and hasattr(fltr, "model_dump")
        and any(value is not None for value in fltr.model_dump().values())
    )
    return ListQuery(
        fltr=fltr if has_filter else None,
        sort=parse_sort_param(sort),
        offset=max(0, offset),
        limit=limit,
    )


async def materialize(records: ListRecords[TItem]) -> list[TItem]:
    """Drains a lazy source into a list. Anything already a list is returned as-is."""
    if isinstance(records, list):
        return records
    if isinstance(records, AsyncIterable):
        return [record async for record in records]
    return list(records)


async def take_page(records: ListRecords[TItem], offset: int, limit: int) -> tuple[list[TItem], bool]:
    """
    One page of `records` and whether more follow, found by reading `limit + 1` rows. A lazy source is
    consumed only up to `offset + limit + 1` items.
    """
    if isinstance(records, list):
        page = records[offset : offset + limit + 1]
        return page[:limit], len(page) > limit

    iterator = _as_async_iterator(records)
    page: list[TItem] = []
    has_more = False
    index = 0
    async for record in iterator:
        if index < offset:
            index += 1
            continue
        if len(page) == limit:
            has_more = True
            break
        page.append(record)
        index += 1
    return page, has_more


async def _as_async_iterator(records: ListRecords[TItem]) -> AsyncIterator[TItem]:
    if isinstance(records, AsyncIterable):
        async for record in records:
            yield record
    else:
        for record in records:
            yield record
