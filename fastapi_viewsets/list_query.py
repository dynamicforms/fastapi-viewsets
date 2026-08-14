"""
What a client asked a list endpoint for, and what it gets back.

`perform_list` used to have to return a materialised `list[T]`, which meant filtering, sorting and
paging could only ever happen in memory: a database-backed viewset had no way to push any of it
down into its query. The `setup_filter`/`setup_sort` hooks existed only to work around that, by
mutating instance state before `perform_list` ran - a pre/post pair that had to be kept in step by
hand.

Now `perform_list` may return any iterable or async iterable, and receives the `ListQuery` so a
backend that can answer part of it directly is free to. Whatever it does not answer, the default
transformations in `ListMixin` still do, in memory. See docs/guide/list-pipeline.md.
"""

from collections.abc import AsyncIterable, AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

TItem = TypeVar("TItem")
"""
Deliberately not the same TypeVar the mixins use for their item type. Pydantic treats `M[X]`,
where X is M's *own* parameter, as a no-op and hands back the bare class - which would strip the
item type out of `PaginatedList[T]` before FastAPI ever saw it.
"""

ListRecords = Iterable[Any] | AsyncIterable[Any]
"""What flows through the list pipeline: anything iterable, lazily or not."""


@dataclass
class ListQuery:
    """
    The list request, as one value rather than four parameters threaded through five methods.

    `applied` is how a backend reports what it already did. A viewset that translated the sort
    into an ORDER BY adds "sort" to it, and the in-memory sort step then knows to leave the rows
    alone instead of redoing the work - which is what makes push-down composable rather than an
    all-or-nothing override of the whole pipeline.
    """

    fltr: Any = None
    sort: list = field(default_factory=list)
    offset: int = 0
    limit: int | None = None
    applied: set[str] = field(default_factory=set)
    cursor: str | None = None
    shape: str | None = None
    """
    Which envelope the answer goes into. `list_items` sets it from the request; left unset - by a
    test, or by one endpoint calling another - `get_list` fills in the viewset's own default, so a
    direct call answers in the shape the viewset advertises rather than in a fixed one.
    """
    extra_filters: list = field(default_factory=list)
    """
    Filters the pipeline adds itself rather than the client asking for them - today only the
    cursor predicate. They ride the same stage as declared filters so a backend translates them
    through the same registry, and decline them the same way.
    """

    @property
    def has_filter(self) -> bool:
        return self.fltr is not None or bool(self.extra_filters)

    @property
    def has_sort(self) -> bool:
        return bool(self.sort)

    @property
    def is_paginated(self) -> bool:
        """
        Pagination is opt-in per request: without a `limit` the endpoint answers with a plain list,
        exactly as it always has, and only a client that asks for a page gets a page envelope.
        """
        return self.limit is not None

    def mark_applied(self, *stages: str) -> None:
        self.applied.update(stages)

    def needs(self, stage: str) -> bool:
        return stage not in self.applied


class PaginatedList(BaseModel, Generic[TItem]):
    """
    One page of results.

    `count` is None when nobody knows it - a generator cannot be counted without draining it, and
    draining it is the thing pagination exists to avoid. `has_more` and `has_previous` are stated
    outright rather than left to be inferred from a null link, because inference is how a client
    ends up guessing wrong at exactly the boundary that matters.
    """

    results: list[TItem]
    offset: int
    limit: int | None
    count: int | None = None
    has_more: bool = False
    has_previous: bool = False


def build_list_query(fltr: Any, sort: str | None, offset: int = 0, limit: int | None = None) -> ListQuery:
    """
    Turns the raw query parameters into a ListQuery.

    A filter model whose every field is None means the client sent no filter at all - FastAPI
    builds the model regardless, so "was a filter asked for" cannot be answered by a null check.
    """
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


async def materialize(records: ListRecords) -> list:
    """Drains a lazy source into a list. Anything already a list is returned as-is."""
    if isinstance(records, list):
        return records
    if isinstance(records, AsyncIterable):
        return [record async for record in records]
    return list(records)


async def take_page(records: ListRecords, offset: int, limit: int) -> tuple[list, bool]:
    """
    Reads one page out of `records`, plus whether anything follows it.

    Reads `limit + 1` rows and reports the extra one as `has_more` rather than returning it. That
    is one row of overhead in exchange for never having to count the whole collection, which for a
    lazy source is the difference between paging and not.

    A list is sliced directly; anything else is consumed only as far as the page needs, so a
    generator over a million rows still costs `offset + limit + 1` steps rather than a million.
    """
    if isinstance(records, list):
        page = records[offset : offset + limit + 1]
        return page[:limit], len(page) > limit

    iterator = _as_async_iterator(records)
    page: list = []
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


async def _as_async_iterator(records: ListRecords) -> AsyncIterator:
    if isinstance(records, AsyncIterable):
        async for record in records:
            yield record
    else:
        for record in records:
            yield record
