import os

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from demo.backend.data_generator import generate_music_library
from demo.backend.django_setup import ensure_database, MusicTrackRow
from fastapi_viewsets.backends.django_orm import DjangoORMViewSet
from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.context import Context
from fastapi_viewsets.filters import make_filter_model
from fastapi_viewsets.mixins import BulkViewSetMixin, CursorListMixin, LookupItem, LookupMixin

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


MusicTrackFilter = make_filter_model(MusicTrack, {
    "id": ["exact"],
    "title": ["icontains"],
    "artist": ["icontains", "exact"],
    "year": ["exact", "gte", "lte", "in"],
    "rating": ["exact", "gte"],
    "favorite": ["exact"],
    "play_count": ["exact", "gte", "lte"],
    "language": ["iexact"],
    "genres": ["overlaps"],
    "moods": ["overlaps"],
})
"""
Declared rather than hand-written: this replaces the boolean expression that used to live in
`filter_list` below, and the SQLite viewset gets the same filters translated into SQL for free.
`genres`/`moods` use `overlaps`, which no backend can translate portably, so a request touching
them falls back to in-memory filtering - correctly, just not as quickly.
"""

_records = generate_music_library(LIBRARY_SIZE)
database: dict[int, MusicTrack] = {record["id"]: MusicTrack(**record) for record in _records}
ensure_database(_records)


class MusicTrackViewSet(
    CollectionViewSet[int, MusicTrack],
    BulkViewSetMixin[int, MusicTrack, MusicTrackFilter],
    CursorListMixin[MusicTrack, MusicTrackFilter],
    LookupMixin,
):
    """
    Cursor-paged rather than offset-paged, because the grid loads by scrolling rather than by page
    number - and because a table being scrolled through while rows arrive is exactly the case
    offset paging gets wrong.
    """

    __router = APIRouter()

    schema = MusicTrack
    pk_field_name = "id"
    default_page_size = 50

    @__router.get("count", tags=["MusicTrack"], summary="Return the total number of tracks")
    async def count(self, context: Context) -> int:
        return len(await self.perform_list(context))

    async def perform_lookup(self, context: Context) -> list[LookupItem]:
        return [LookupItem(group=None, pk=t.id, title=t.title, icon=None) for t in await self.perform_list(context)]

    def __init__(self):
        super().__init__(container=database, pk_field="id")


class MusicTrackDbViewSet(
    DjangoORMViewSet[int, MusicTrack],
    CursorListMixin[MusicTrack, MusicTrackFilter],
):
    """
    The same API as MusicTrackViewSet, over SQLite instead of a dict.

    Worth watching while scrolling: the in-memory one walks the collection to find each page
    boundary, this one seeks to it - and with `year` in the ordering, the composite index on
    (year, id) is exactly what the cursor's row-value comparison needs.
    """

    model = MusicTrackRow
    schema = MusicTrack
    pk_field_name = "id"
    default_page_size = 50

    def to_record(self, raw: Any) -> MusicTrack:
        if isinstance(raw, MusicTrack):
            return raw
        return MusicTrack(
            id=raw.id,
            title=raw.title,
            artist=raw.artist,
            year=raw.year,
            duration=raw.duration,
            genres=raw.genres.split(",") if raw.genres else [],
            rating=raw.rating,
            favorite=raw.favorite,
            play_count=raw.play_count,
            moods=raw.moods.split(",") if raw.moods else [],
            language=raw.language,
        )


if USE_CELERY:
    from demo.backend.celery_app import celery_app, redis_sync
    from fastapi_viewsets.decorators.celery_viewset import celery_viewset

    MusicTrackViewSet = celery_viewset(  # noqa: F811 - same viewset, wrapped for the Celery path
        celery_app=celery_app, task_prefix="music", redis_client=redis_sync
    )(MusicTrackViewSet)
