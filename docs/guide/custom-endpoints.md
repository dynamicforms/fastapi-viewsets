# Custom Endpoints

Mixins automatically register standard CRUD routes. When you need an endpoint that doesn't fit the standard pattern, you
can add it manually — both on the backend and the frontend.

---

## Backend (Python / FastAPI)

### How mixins register routes

Each mixin declares a class-level `__router` (`APIRouter`) and decorates its methods with standard FastAPI route decorators:

```python
class ListMixin(Generic[T], ABC):
    __router = APIRouter()

    @final
    @__router.get("")
    async def list_items(self: ..., context: Context) -> list[T]:
        return await self.perform_list(context)
```

`route_viewset` collects all `__router` instances from the MRO and registers their routes on the application router.

### Adding a custom route

Declare your own `__router` on the ViewSet class and decorate a method with it. `route_viewset` will pick it up
automatically alongside the mixin routes:

```python
from fastapi import APIRouter
from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.context import Context
from fastapi_viewsets.decorators.route_viewset import route_viewset
from fastapi_viewsets.mixins import BulkViewSetMixin, LookupItem, LookupMixin

router = APIRouter()

@route_viewset(router, base_path="/items", pk_field_name="id")
class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item], LookupMixin):
    __router = APIRouter()  # custom router for this class

    def __init__(self):
        super().__init__(container=database, pk_field="id")

    async def perform_lookup(self, context: Context) -> list[LookupItem]:
        return [LookupItem(group=None, pk=item.id, title=item.name, icon=None)
                for item in await self.perform_list(context)]

    @__router.get("search", tags=["Item"])
    async def search(self, context: Context, q: str) -> list[Item]:
        """Return items whose name contains the query string."""
        all_items = await self.perform_list(context)
        return [item for item in all_items if q.lower() in item.name.lower()]
```

This registers `GET /items/search?q=...` in addition to all standard mixin routes.

A custom endpoint that declares `request: Request` receives FastAPI's `Request`.

A viewset needs no mixin. A class with a `__router` only serves its custom endpoints and
`GET {base_path}/schema`; its FE counterpart is in
[A ViewSet with custom endpoints only](./vue-custom-endpoints#a-viewset-with-custom-endpoints-only).

An endpoint that is a whole viewset of its own, another row type over the same data with its own
list shapes and filters, is mounted with `ViewSetRouter.sub_viewset` instead; see
[Nested Viewsets](./nested-viewsets).

### Custom POST endpoint with a request body

```python
from pydantic import BaseModel

class CloneRequest(BaseModel):
    source_id: int
    new_name: str

@route_viewset(router, base_path="/items", pk_field_name="id")
class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item]):
    __router = APIRouter()

    def __init__(self):
        super().__init__(container=database, pk_field="id")

    @__router.post("clone", tags=["Item"])
    async def clone(self, context: Context, body: CloneRequest) -> Item:
        """Clone an existing item under a new name."""
        source = await self.perform_retrieve(context, body.source_id)
        return await self.perform_create(
            context, Item(id=0, name=body.new_name, description=source.description)
        )
```

### Files

A custom endpoint receives a file through an `UploadFile` parameter, sent as `multipart/form-data`
(FastAPI reads it with `python-multipart`, a dependency of this package). It answers with a file by
returning a starlette `Response` whose body is the file's bytes:

```python
from urllib.parse import quote

from fastapi import UploadFile
from starlette.responses import Response

from fastapi_viewsets.mixins import ReadOnlyViewSetMixin


@route_viewset(router, base_path="/tracks", pk_field_name="id")
class TrackViewSet(CollectionViewSet[int, Track], ReadOnlyViewSetMixin[int, Track]):
    __router = APIRouter()

    def __init__(self):
        super().__init__(container=tracks, pk_field="id")

    @__router.post("attachments")
    async def upload_attachment(self, context: Context, file: UploadFile) -> Attachment:
        """Store an attachment and report what was received."""
        data = await file.read()
        return Attachment(filename=file.filename or "", size=len(data))

    @__router.get("{pk}/pdf")
    async def pdf(self, context: Context, pk: int) -> Response:
        """The track as a PDF."""
        track = await self.perform_retrieve(context, pk)  # unknown pk: 404, fastapi-viewsets.not_found
        name = f"track-{pk}-é.pdf"
        return Response(
            content=render_pdf(track),
            media_type="application/pdf",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="track-{pk}.pdf"; filename*=UTF-8\'\'{quote(name)}'
                )
            },
        )
```

`filename` in `Content-Disposition` is ASCII; `filename*` (RFC 5987) carries a name with other
characters, and the TS client prefers it (see
[Downloading a file](./vue-custom-endpoints#downloading-a-file)).

- [`Idempotency`](./idempotency#rules) records a `Response` with a buffered body (bytes and headers)
  and answers a repeat with it. A `FileResponse` or `StreamingResponse` is not recorded, and a
  repeat runs the action again.
- A [`celery_viewset`](./routers#arguments-and-results) action cannot return a `FileResponse` or a
  `StreamingResponse`; it returns `Response(content=bytes)`.
- A page on another origin reads `Content-Disposition` only when the server exposes it:
  `CORSMiddleware(..., expose_headers=["Content-Disposition"])`.

### Notes

- The `__router` attribute on the ViewSet class **must not** shadow the one inherited from a mixin. Declare it as a
  fresh `APIRouter()`, or a `ViewSetRouter()` to also mount sub-viewsets, directly on your class.
- Route paths are relative to `base_path`. `"search"` becomes `/items/search`.
- All standard FastAPI route parameters (`tags`, `summary`, `response_model`, `status_code`, …) are supported.
- A method annotated to return a `Response` class (`-> Response`, `-> FileResponse`, …) has no response model: the
  response it returns is sent as it is, with its own status code, headers and body.
- The method receives `self` — the viewset instance — so you have full access to `perform_*` helpers and any other
  instance state.
