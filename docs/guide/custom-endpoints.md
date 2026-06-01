# Custom Endpoints

Mixins automatically register standard CRUD routes. When you need an endpoint that doesn't fit the standard pattern, you can add it manually — both on the backend and the frontend.

---

## Backend (Python / FastAPI)

### How mixins register routes

Each mixin declares a class-level `__router` (`APIRouter`) and decorates its methods with standard FastAPI route decorators:

```python
class ListMixin(Generic[T], ABC):
    __router = APIRouter()

    @final
    @__router.get("")
    async def list_items(self: ...) -> list[T]:
        return await self.perform_list()
```

`route_viewset` collects all `__router` instances from the MRO and registers their routes on the application router.

### Adding a custom route

Declare your own `__router` on the ViewSet class and decorate a method with it. `route_viewset` will pick it up automatically alongside the mixin routes:

```python
from fastapi import APIRouter
from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.decorators.route_viewset import route_viewset
from fastapi_viewsets.mixins import BulkViewSetMixin, LookupItem, LookupMixin

router = APIRouter()

@route_viewset(router, base_path="/items", pk_field_name="id")
class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item], LookupMixin):
    __router = APIRouter()  # custom router for this class

    def __init__(self):
        super().__init__(container=database, pk_field="id")

    async def perform_lookup(self) -> list[LookupItem]:
        return [LookupItem(group=None, pk=item.id, title=item.name, icon=None)
                for item in await self.perform_list()]

    @__router.get("search", tags=["Item"])
    async def search(self, q: str) -> list[Item]:
        """Return items whose name contains the query string."""
        all_items = await self.perform_list()
        return [item for item in all_items if q.lower() in item.name.lower()]
```

This registers `GET /items/search?q=...` in addition to all standard mixin routes.

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
    async def clone(self, body: CloneRequest) -> Item:
        """Clone an existing item under a new name."""
        source = await self.perform_retrieve(body.source_id)
        return await self.perform_create(
            Item(id=0, name=body.new_name, description=source.description)
        )
```

### Notes

- The `__router` attribute on the ViewSet class **must not** shadow the one inherited from a mixin. Declare it as a fresh `APIRouter()` directly on your class.
- Route paths are relative to `base_path`. `"search"` becomes `/items/search`.
- All standard FastAPI route parameters (`tags`, `summary`, `response_model`, `status_code`, …) are supported.
- The method receives `self` — the viewset instance — so you have full access to `perform_*` helpers and any other instance state.

