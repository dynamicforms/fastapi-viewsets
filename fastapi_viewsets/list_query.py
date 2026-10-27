"""
The list request (`ListQuery`) and answer types. `perform_list` returns `ListRecords`; `apply_filter`,
`apply_sort`, `take_page` and `count_records` receive the `ListQuery`, so a backend pushes part of the
query down by overriding one of them. See docs/guide/list-pipeline.md.
"""

from collections.abc import AsyncIterable, AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

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
    sort: list = field(default_factory=list)
    offset: int = 0
    limit: int | None = None
    applied: set[str] = field(default_factory=set)
    cursor: str | None = None
    shape: str | None = None
    """The answer envelope; when unset, `get_list` uses the viewset's default shape."""
    extra_filters: list = field(default_factory=list)
    """Filters the pipeline adds itself (the cursor predicate), applied in the filter stage."""

    @property
    def has_filter(self) -> bool:
        return self.fltr is not None or bool(self.extra_filters)

    @property
    def has_sort(self) -> bool:
        return bool(self.sort)

    @property
    def is_paginated(self) -> bool:
        """Whether the request sent a `limit`."""
        return self.limit is not None

    def mark_applied(self, *stages: str) -> None:
        self.applied.update(stages)

    def needs(self, stage: str) -> bool:
        return stage not in self.applied


class PaginatedList(BaseModel, Generic[TItem]):
    """One page of results; `count` is None when the source cannot be counted without draining it."""

    results: list[TItem]
    offset: int
    limit: int | None
    count: int | None = None
    has_more: bool = False
    has_previous: bool = False


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
