import os

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from demo.backend.data_generator import generate_music_library
from demo.backend.django_setup import MusicTrackRow
from fastapi_viewsets.backends.django_orm import DjangoORMViewSet
from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.conf import settings
from fastapi_viewsets.context import Context
from fastapi_viewsets.endpoint_docs import endpoint_docs
from fastapi_viewsets.events import ChangeEvents, MemoryEventStore, ViewsetTopics
from fastapi_viewsets.filters import make_filter_model
from fastapi_viewsets.mixins import (
    BulkViewSetMixin,
    CursorListMixin,
    LookupItem,
    LookupMixin,
    RetrieveMixin,
    UpdateMixin,
)

USE_CELERY = os.environ.get("DEMO_CELERY", "").lower() in ("1", "true", "yes")
"""
DEMO_CELERY=1 (with a running worker) dispatches every call through Celery. Off by default: the Redis round trip
would dominate the HTTP/WebSocket difference the benchmark measures.
"""

# Writes publish `records_changed` to the viewset's base path topic. Set before any viewset is decorated, in
# both processes: with Celery, the worker publishes.
settings.viewsets_command_middleware = [ChangeEvents()]
# A caller may follow a viewset's topic when it may call the viewset (here: everyone, no authentication).
settings.viewsets_event_authorizers = [ViewsetTopics()]

if USE_CELERY:
    from demo.backend.celery_app import redis_async
    from fastapi_viewsets.events.redis import RedisEventStore

    # The worker publishes into the same Redis the web process polls and reads pushes from.
    settings.viewsets_event_store = RedisEventStore(redis_async)
else:
    settings.viewsets_event_store = MemoryEventStore()

MEMORY_TASK_PREFIX = "music"
"""The Celery task names of `MusicTrackViewSet` under DEMO_CELERY: `music.<action>`."""
DB_TASK_PREFIX = "music_db"
"""The Celery task names of `MusicTrackDbViewSet` under DEMO_CELERY: `music_db.<action>`."""

LIBRARY_SIZE = int(os.environ.get("DEMO_LIBRARY_SIZE", "5000"))
"""Number of generated tracks; large enough to need paging."""


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


MusicTrackFilter = make_filter_model(
    MusicTrack,
    {
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
    },
)
"""Declared filters; the SQLite viewset translates them into SQL except `overlaps`, which runs in memory."""

records = generate_music_library(LIBRARY_SIZE)
database: dict[int, MusicTrack] = {record["id"]: MusicTrack(**record) for record in records}
"""
The in-memory library, one copy per process (so `demo.py --celery` runs the worker as one process).

The SQLite copy is seeded from the lifespan in a worker thread: uvicorn imports `"module:app"` inside its event
loop, where Django refuses a synchronous query.
"""


@endpoint_docs(
    {
        "list_items": {
            "summary": "Browse the library",
            "description": (
                "Cursor-paged by default - follow `next` to walk forward. Send `X-List-Shape: plain` "
                "for a bare array or `paginated` for offset paging; the response examples below are "
                "named after the value that produces them."
            ),
        },
        "retrieve": {"summary": "One track by id"},
        "create": {"summary": "Add a track"},
        "bulk_create": {"summary": "Add several tracks at once"},
        "lookup": {"summary": "Titles and ids, for a select box"},
        "count": {"summary": "How many tracks there are"},
    }
)
class MusicTrackViewSet(
    CollectionViewSet[int, MusicTrack],
    BulkViewSetMixin[int, MusicTrack, MusicTrackFilter],
    CursorListMixin[MusicTrack, MusicTrackFilter],
    LookupMixin,
):
    """
    The music library, held in memory.

    Served from a Python dict by `CollectionViewSet`; **Music Track Db** exposes the same API over SQLite.
    Listing is cursor-paged by default; send `X-List-Shape: plain` or `paginated` for the other shapes.
    Filters such as `year__gte`, `title__icontains` and `genres__overlaps` are declared, not coded.
    """

    __router = APIRouter()

    schema = MusicTrack
    pk_field_name = "id"
    default_page_size = 50

    list_shapes = ("cursor", "plain", "paginated")
    """All three, to show the choice in Swagger and ReDoc; one shape keeps the schema to a single model."""

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
    RetrieveMixin[int, MusicTrack],
    UpdateMixin[int, MusicTrack],
):
    """
    The same music library, over SQLite through the Django ORM.

    Filters, sorts and the page are translated into `WHERE`, `ORDER BY` and `LIMIT`; anything it cannot
    translate falls back to the in-memory pipeline. The cursor seeks with a row-value comparison on the
    (year, id) index.
    """

    model = MusicTrackRow
    schema = MusicTrack
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
        celery_app=celery_app, task_prefix=MEMORY_TASK_PREFIX, redis_client=redis_sync
    )(MusicTrackViewSet)
    MusicTrackDbViewSet = celery_viewset(  # noqa: F811 - same viewset, wrapped for the Celery path
        celery_app=celery_app, task_prefix=DB_TASK_PREFIX, redis_client=redis_sync
    )(MusicTrackDbViewSet)
