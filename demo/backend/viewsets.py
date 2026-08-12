import os

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from demo.backend.data_generator import generate_music_library
from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.context import Context
from fastapi_viewsets.mixins import (
    BulkViewSetMixin,
    LookupItem,
    LookupMixin,
    make_all_optional,
    PaginatedListMixin,
)

USE_CELERY = os.environ.get("DEMO_CELERY", "").lower() in ("1", "true", "yes")
"""
Celery is off by default in this demo.

It used to be unconditional, which made the transport benchmark meaningless: every call queued a
task through Redis and waited for a worker, and that round trip dwarfs the difference between
sending a request over HTTP and sending it over a WebSocket - which is the thing the benchmark
exists to show. Set DEMO_CELERY=1 (and run the worker) to exercise the Celery path instead.
"""

LIBRARY_SIZE = int(os.environ.get("DEMO_LIBRARY_SIZE", "5000"))
"""Large enough that paging is the only sane way to read it - which is the point of the demo."""


class MusicTrack(BaseModel):
    id: int = Field(json_schema_extra={"autoinc_int": True})
    title: str
    artist: str
    year: int
    duration: str
    genres: list[str]
    rating: int
    favorite: bool
    play_count: int
    moods: list[str]
    language: str


MusicTrackFilter = make_all_optional(MusicTrack)

database: dict[int, MusicTrack] = {
    record["id"]: MusicTrack(**record) for record in generate_music_library(LIBRARY_SIZE)
}


class MusicTrackViewSet(
    CollectionViewSet[int, MusicTrack],
    BulkViewSetMixin[int, MusicTrack, MusicTrackFilter],
    PaginatedListMixin[MusicTrack, MusicTrackFilter],
    LookupMixin,
):
    __router = APIRouter()

    default_page_size = 50

    @__router.get("count", tags=["MusicTrack"], summary="Return the total number of tracks")
    async def count(self, context: Context) -> int:
        return len(await self.perform_list(context))

    async def perform_lookup(self, context: Context) -> list[LookupItem]:
        return [LookupItem(group=None, pk=t.id, title=t.title, icon=None) for t in await self.perform_list(context)]

    async def filter_list(self, fltr: Any, items: list[MusicTrack]) -> list[MusicTrack]:
        def filter_item(itm: MusicTrack) -> bool:
            return (
                (fltr.id is None or itm.id == fltr.id)
                and (fltr.title is None or fltr.title.lower() in itm.title.lower())
                and (fltr.artist is None or fltr.artist.lower() in itm.artist.lower())
                and (fltr.year is None or itm.year == fltr.year)
                and (fltr.duration is None or itm.duration == fltr.duration)
                and (fltr.genres is None or set(fltr.genres).intersection(itm.genres))
                and (fltr.rating is None or (itm.rating == fltr.rating))
                and (fltr.favorite is None or (itm.favorite == fltr.favorite))
                and (fltr.play_count is None or (itm.play_count == fltr.play_count))
                and (fltr.moods is None or set(fltr.moods).intersection(itm.moods))
                and (fltr.language is None or itm.language.lower() == fltr.language.lower())
            )

        return list(filter(filter_item, items))

    def __init__(self):
        super().__init__(container=database, pk_field="id")


if USE_CELERY:
    from demo.backend.celery_app import celery_app, redis_sync
    from fastapi_viewsets.decorators.celery_viewset import celery_viewset

    MusicTrackViewSet = celery_viewset(  # noqa: F811 - same viewset, wrapped for the Celery path
        celery_app=celery_app, task_prefix="music", redis_client=redis_sync
    )(MusicTrackViewSet)
