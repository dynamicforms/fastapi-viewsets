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
Celery is off by default in this demo.

With Celery, every call queues a task through Redis and waits for a worker; that round trip
dominates the difference between HTTP and WebSocket transport that the benchmark measures.
Set DEMO_CELERY=1 (and run the worker) to exercise the Celery path.
"""

# Every create, update and destroy of a viewset publishes `records_changed` to the viewset's topic,
# its routed base path (`/music`, `/music-db`). Set here, before any viewset is decorated, and in
# both processes: with Celery, the worker runs the action and publishes the event.
settings.viewsets_command_middleware = [ChangeEvents()]
# A caller may follow a viewset's topic when it may call the viewset. The demo has no
# authentication, so every caller may follow both.
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
"""
Declared filters: the SQLite viewset receives the same filters translated into SQL. `genres`/`moods`
use `overlaps`, which no backend translates, so a request with them is filtered in memory.
"""

records = generate_music_library(LIBRARY_SIZE)
database: dict[int, MusicTrack] = {record["id"]: MusicTrack(**record) for record in records}
"""
The in-memory library. Pure Python, so building it at import time costs nothing but CPU.

Every process that imports this module holds its own dict, so `demo.py --celery` runs the worker as
one process. `MusicTrackDbViewSet` keeps its rows in the SQLite file, which every process shares.

The SQLite copy is *not* seeded here: `ensure_database` talks to the database, and uvicorn imports
the app inside its own event loop when it is given `"module:app"` as a string - where Django
refuses a synchronous query outright. It is seeded from the lifespan instead, in a worker thread.
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

    Everything here is served from a plain Python dict, which is what `CollectionViewSet` is for:
    no database, no setup, and every stage of the list pipeline running in memory. **Music Track
    Db** exposes the identical API over SQLite through the Django ORM - same records, same
    ordering, same cursors - so the two can be compared side by side.

    Listing is **cursor-paged** by default: the demo's grid loads by scrolling rather than by page
    number, and a table being scrolled while rows arrive is exactly the case offset paging gets
    wrong. Send `X-List-Shape: plain` or `paginated` to get one of the other two shapes instead.

    Filtering and sorting are declared rather than written - see the `year__gte`, `title__icontains`
    and `genres__overlaps` parameters, none of which has any code behind it.
    """

    __router = APIRouter()

    schema = MusicTrack
    pk_field_name = "id"
    default_page_size = 50

    list_shapes = ("cursor", "plain", "paginated")
    """
    All three, so the demo can show what the choice looks like in Swagger and ReDoc. A real viewset
    usually declares none of this and answers in one shape - which keeps its schema to one model
    instead of a union every client has to narrow.
    """

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

    Lists, retrieves and updates the same records as **Music Track**, on a backend that can answer
    part of a list query itself: exact filters become `WHERE` clauses, ascending sorts become
    `ORDER BY`, and the page becomes `LIMIT`/`OFFSET` - each reported back so the in-memory pipeline
    does not redo it.
    Anything it cannot translate falls back rather than failing, which is the contract.

    Worth watching while scrolling: the in-memory one walks the collection to find each page
    boundary, this one seeks to it - and with `year` in the ordering, the composite index on
    (year, id) is exactly what the cursor's row-value comparison needs.
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
