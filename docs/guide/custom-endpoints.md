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

---

## Frontend (TypeScript / Vue)

### How the proxy works

`RestProxyImpl` (and `route_rest`) map fixed method names (`list`, `retrieve`, `create`, …) to HTTP calls. For a custom endpoint that doesn't match any of those, extend `RestProxyImpl` and add the method yourself.

### Extending RestProxyImpl

```ts
import axios from 'axios';
import { RestProxyImpl } from '@dynamicforms/viewsets';
import type { BulkViewSetMixin } from '@dynamicforms/viewsets';

interface Item { id: number; name: string; description: string | null }

interface CloneRequest { source_id: number; new_name: string }

class ItemApi extends RestProxyImpl<number, Item, 'id'> {
  /** Search items by name fragment. */
  async search(q: string): Promise<Item[]> {
    const res = await this.http.get<Item[]>(`${this.basePath}/search`, { params: { q } });
    return res.data;
  }

  /** Clone an item under a new name. */
  async clone(body: CloneRequest): Promise<Item> {
    const res = await this.http.post<Item>(`${this.basePath}/clone`, body);
    return res.data;
  }
}

// Instantiate directly (no route_rest needed for subclasses):
const itemsApi = new ItemApi({ basePath: '/items', pkFieldName: 'id' });

// Standard mixin methods still work:
const all = await itemsApi.list();
const one = await itemsApi.retrieve(1);

// Custom methods:
const results  = await itemsApi.search('widget');
const cloned   = await itemsApi.clone({ source_id: 1, new_name: 'Widget copy' });
```

### Accessing protected members

`RestProxyImpl` exposes two `protected` members you can use inside subclass methods:

| Member | Type | Description |
|--------|------|-------------|
| `this.http` | `AxiosInstance` | The axios instance (custom or global) |
| `this.basePath` | `string` | The base path, e.g. `'/items'` |

### Using a custom axios instance

```ts
const http = axios.create({
  baseURL: 'https://api.example.com',
  headers: { Authorization: 'Bearer my-token' },
});

const itemsApi = new ItemApi({ basePath: '/items', pkFieldName: 'id', axiosInstance: http });
```

### Type-safe declaration

If you want the extended API to be typed as a union of the mixin interface and your custom methods, declare an interface:

```ts
interface ItemApiInterface extends BulkViewSetMixin<number, Item, 'id'> {
  search(q: string): Promise<Item[]>;
  clone(body: CloneRequest): Promise<Item>;
}

const itemsApi: ItemApiInterface = new ItemApi({ basePath: '/items', pkFieldName: 'id' });
```
