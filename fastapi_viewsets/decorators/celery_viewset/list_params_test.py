"""
The list pipeline's parameters crossing into a Celery worker. `celery_viewset_client` patches the
route endpoints (`list_items`, not `perform_list`), so filter, sort and pagination run in the worker.
"""

from unittest.mock import MagicMock

from pydantic import BaseModel, Field

from fastapi_viewsets.decorators import celery_viewset_server
from fastapi_viewsets.decorators.celery_viewset.transport import ActionCodec
from fastapi_viewsets.list_query import ListQuery, ListRecords
from fastapi_viewsets.mixins import make_all_optional, PaginatedListMixin


class Track(BaseModel):
    id: int = Field(json_schema_extra={"autoinc_int": True})
    title: str
    year: int


TrackFilter = make_all_optional(Track)

LIBRARY = [Track(id=n, title=f"Track {n:03d}", year=2000 + (n % 5)) for n in range(1, 31)]


def worker_tasks_for(viewset_factory) -> dict:
    """Registers a viewset against a mock Celery app and hands back its task functions."""
    celery_app = MagicMock()
    registered: dict = {}

    def mock_task(name, **_kwargs):
        def decorator(func):
            registered[name] = func
            return func

        return decorator

    celery_app.task.side_effect = mock_task
    viewset_factory(celery_viewset_server(celery_app=celery_app, task_prefix="tracks", redis_client=MagicMock()))
    return registered


def paged_viewset(decorate):
    @decorate
    class TrackViewSet(PaginatedListMixin[Track, TrackFilter]):
        default_page_size = 10

        async def perform_list(self, _context) -> list[Track]:
            return list(LIBRARY)

        async def filter_list(self, fltr, records):
            return [track for track in records if fltr.year is None or track.year == fltr.year]

    return TrackViewSet


def test_pagination_parameters_reach_the_worker():
    task = worker_tasks_for(paged_viewset)["tracks.list_items"]
    page = task(offset=5, limit=3)
    assert [track.id for track in page.results] == [6, 7, 8]
    assert page.has_more is True
    assert page.has_previous is True


def test_the_sort_parameter_reaches_the_worker():
    task = worker_tasks_for(paged_viewset)["tracks.list_items"]
    page = task(sort="id:desc", limit=3)
    assert [track.id for track in page.results] == [30, 29, 28]


def test_the_filter_model_survives_the_round_trip_as_a_dict():
    """The worker rebuilds the JSON filter as the filter model from the endpoint's type hints."""
    task = worker_tasks_for(paged_viewset)["tracks.list_items"]
    page = task(fltr={"year": 2003}, limit=50)
    assert page.results
    assert all(track.year == 2003 for track in page.results)


def test_filter_sort_and_paging_compose_in_the_worker():
    task = worker_tasks_for(paged_viewset)["tracks.list_items"]
    page = task(fltr={"year": 2003}, sort="id:desc", offset=1, limit=2)
    assert [track.id for track in page.results] == [23, 18]
    assert page.has_more is True


def test_the_default_page_size_applies_in_the_worker():
    task = worker_tasks_for(paged_viewset)["tracks.list_items"]
    assert len(task().results) == 10


def test_a_stage_the_backend_handled_is_not_redone_in_the_worker():
    """mark_applied() holds in the worker: a stage the backend applied is not redone in memory."""

    def pushdown_viewset(decorate):
        @decorate
        class PushdownViewSet(PaginatedListMixin[Track, TrackFilter]):
            async def perform_list(self, _context) -> list[Track]:
                return list(reversed(LIBRARY))

            async def apply_sort(self, context, query: ListQuery, records: ListRecords) -> ListRecords:
                query.mark_applied("sort")
                return await super().apply_sort(context, query, records)

        return PushdownViewSet

    task = worker_tasks_for(pushdown_viewset)["tracks.list_items"]
    page = task(sort="id:asc", limit=3)
    assert [track.id for track in page.results] == [30, 29, 28]


def _encoded_result(shape: str, **kwargs):
    viewsets = []

    def factory(decorate):
        viewset = paged_viewset(lambda cls: cls)
        viewset.list_shapes = (shape,)
        viewsets.append(decorate(viewset))
        return viewsets[0]

    task = worker_tasks_for(factory)["tracks.list_items"]
    viewset = viewsets[0]
    return ActionCodec(viewset, viewset.list_items).encode_result(task(**kwargs))


def test_a_page_encodes_as_the_declared_result():
    data, encoding = _encoded_result("paginated", offset=5, limit=3)
    assert encoding == "typed"
    assert [track["id"] for track in data["results"]] == [6, 7, 8]


def test_a_cursor_page_encodes_as_the_declared_result():
    data, encoding = _encoded_result("cursor", limit=3)
    assert encoding == "typed"
    assert [track["id"] for track in data["results"]] == [1, 2, 3]
