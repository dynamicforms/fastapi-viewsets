"""The music library on PostgreSQL through adorm; imported only when `adorm_demo.ENABLED`."""

import adorm

from fastapi import APIRouter, HTTPException

from demo.backend import adorm_demo
from demo.backend.adorm_models.backend import MusicTrackRecord
from demo.backend.viewsets import LIST_ITEMS_DESCRIPTION
from fastapi_viewsets.backends.adorm import AdormViewSet
from fastapi_viewsets.context import Context
from fastapi_viewsets.endpoint_docs import endpoint_docs
from fastapi_viewsets.filters import make_filter_model
from fastapi_viewsets.mixins import BulkViewSetMixin, CursorListMixin, LookupMixin

MusicTrackRecordFilter = make_filter_model(
    MusicTrackRecord,
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
"""The filters of the other two viewsets; adorm translates them into SQL except `overlaps`, which runs in memory."""


@endpoint_docs(
    {
        "list_items": {"summary": "Browse the library on PostgreSQL", "description": LIST_ITEMS_DESCRIPTION},
        "retrieve": {"summary": "One track by id"},
        "create": {"summary": "Add a track"},
        "bulk_create": {"summary": "Add several tracks at once"},
        "lookup": {"summary": "Titles and ids, for a select box"},
        "count": {"summary": "How many tracks there are"},
    }
)
class MusicTrackAdormViewSet(
    AdormViewSet[int, MusicTrackRecord],
    BulkViewSetMixin[int, MusicTrackRecord, MusicTrackRecordFilter],
    CursorListMixin[MusicTrackRecord, MusicTrackRecordFilter],
    LookupMixin,
):
    """
    The same music library, on PostgreSQL through adorm.

    The records are the generated adorm model `MusicTrackRecord`. Filters, sorts and the page are translated into
    `WHERE`, `ORDER BY` and `LIMIT` of adorm queries, awaited on adorm's pool without a thread per request; writes
    run in one transaction each, and a violated constraint answers a field error.
    """

    __router = APIRouter()

    model = MusicTrackRecord
    default_page_size = 50
    list_shapes = ("cursor", "plain", "paginated")
    lookup_title = "title"

    async def get_session(self, context: Context) -> adorm.Session:
        """The current session; 503 when the PostgreSQL server could not be set up at startup."""
        if not adorm_demo.ENABLED:
            raise HTTPException(503, f"The adorm demo is off: {adorm_demo.UNAVAILABLE}")
        return await super().get_session(context)

    @__router.get("count", summary="Return the total number of tracks")
    async def count(self, context: Context) -> int:
        queryset = await self.get_queryset(context)
        return await queryset.count()
