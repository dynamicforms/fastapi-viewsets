import pytest

from pydantic import BaseModel

from .list_query import build_list_query, ListQuery, materialize, PaginatedList, take_page


class Item(BaseModel):
    id: int


def items(count: int) -> list[Item]:
    return [Item(id=n) for n in range(count)]


def generator(count: int):
    for n in range(count):
        yield Item(id=n)


async def async_generator(count: int):
    for n in range(count):
        yield Item(id=n)


# ---------------------------------------------------------------------------
# ListQuery
# ---------------------------------------------------------------------------


def test_a_filter_model_of_all_nones_does_not_count_as_a_filter():
    """FastAPI builds the filter model whether or not the client sent anything in it."""

    class Filter(BaseModel):
        id: int | None = None
        name: str | None = None

    assert build_list_query(Filter(), None).has_filter is False
    assert build_list_query(Filter(id=1), None).has_filter is True


def test_a_negative_offset_is_clamped_rather_than_wrapping():
    """Left alone it would index from the end of a list, quietly returning the wrong page."""
    assert build_list_query(None, None, offset=-5).offset == 0


def test_applied_stages_are_skipped_by_later_ones():
    query = ListQuery(sort=[object()])
    assert query.needs("sort")
    query.mark_applied("sort")
    assert not query.needs("sort")


def test_pagination_is_off_without_a_limit():
    assert build_list_query(None, None).is_paginated is False
    assert build_list_query(None, None, limit=10).is_paginated is True


# ---------------------------------------------------------------------------
# materialize
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_passes_a_list_straight_through():
    source = items(3)
    assert await materialize(source) is source


@pytest.mark.asyncio
async def test_materialize_drains_both_kinds_of_generator():
    assert len(await materialize(generator(3))) == 3
    assert len(await materialize(async_generator(3))) == 3


# ---------------------------------------------------------------------------
# take_page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_take_page_slices_a_list():
    page, has_more = await take_page(items(10), offset=2, limit=3)
    assert [item.id for item in page] == [2, 3, 4]
    assert has_more is True


@pytest.mark.asyncio
async def test_take_page_reports_the_last_page_as_having_no_more():
    page, has_more = await take_page(items(10), offset=7, limit=3)
    assert [item.id for item in page] == [7, 8, 9]
    assert has_more is False


@pytest.mark.asyncio
async def test_take_page_past_the_end_is_empty_rather_than_an_error():
    page, has_more = await take_page(items(3), offset=99, limit=10)
    assert page == []
    assert has_more is False


@pytest.mark.asyncio
async def test_take_page_walks_a_sync_generator():
    page, has_more = await take_page(generator(10), offset=2, limit=3)
    assert [item.id for item in page] == [2, 3, 4]
    assert has_more is True


@pytest.mark.asyncio
async def test_take_page_walks_an_async_generator():
    page, has_more = await take_page(async_generator(10), offset=0, limit=4)
    assert [item.id for item in page] == [0, 1, 2, 3]
    assert has_more is True


@pytest.mark.asyncio
async def test_take_page_stops_reading_once_the_page_is_full():
    """
    The whole point of paging a lazy source: a page out of a million rows must cost
    offset + limit + 1 steps, not a million.
    """
    consumed = 0

    def counting():
        nonlocal consumed
        for n in range(1_000_000):
            consumed += 1
            yield Item(id=n)

    page, has_more = await take_page(counting(), offset=5, limit=10)
    assert len(page) == 10
    assert has_more is True
    assert consumed == 16  # 5 skipped + 10 taken + 1 lookahead


# ---------------------------------------------------------------------------
# PaginatedList
# ---------------------------------------------------------------------------


def test_paginated_list_states_its_edges_rather_than_implying_them():
    page = PaginatedList[Item](results=items(3), offset=0, limit=3, count=10, has_more=True)
    assert page.has_more is True
    assert page.has_previous is False
    assert page.count == 10


def test_count_may_be_unknown():
    """A generator cannot be counted without draining it, which is what paging exists to avoid."""
    page = PaginatedList[Item](results=items(3), offset=0, limit=3)
    assert page.count is None
