# Getting Started

## Installation

### Python (FastAPI backend)

```bash
pip install fastapi-viewsets
```

Requirements: Python 3.11+, FastAPI, Pydantic v2.

### Vue / TypeScript (frontend)

```bash
npm install @dynamicforms/viewsets
```

Requirements: Vue 3.4+, axios.

---

## Quick Start

### 1. Define your Pydantic model

```python
from pydantic import BaseModel, Field

class Item(BaseModel):
    id: int = Field(json_schema_extra={"autoinc_int": True})
    name: str
    description: str | None = None
```

The `autoinc_int` extra flag tells `CollectionViewSet` to auto-assign incrementing integer IDs on create.

### 2. Prepare your data container

```python
database: dict[int, Item] = {
    1: Item(id=1, name="First element", description="This is the description of the first element"),
    2: Item(id=2, name="Second element", description="Another description"),
}
```

Any `list`, `set`, or `dict` works. A `dict` keyed by PK is recommended for fast lookups.

### 3. Define the ViewSet

Inherit from `CollectionViewSet` (the built-in implementation) and the mixin(s) that declare
which HTTP operations you want. No `perform_*` methods needed — `CollectionViewSet` provides them all.
Only add methods for genuinely custom behaviour (e.g. `perform_lookup`):

```python
from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.mixins import BulkViewSetMixin, LookupItem, LookupMixin

class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item], LookupMixin):
    async def perform_lookup(self) -> list[LookupItem]:
        return [LookupItem(group=None, pk=item.id, title=item.name, icon=None)
                for item in await self.perform_list()]

    def __init__(self):
        super().__init__(container=database, pk_field="id")
```

### 4. Register the ViewSet

```python
from fastapi import APIRouter, FastAPI
from fastapi_viewsets.decorators.route_viewset import route_viewset

app = FastAPI()
router = APIRouter()

@route_viewset(router, base_path="/items", pk_field_name="id")
class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item], LookupMixin):
    async def perform_lookup(self) -> list[LookupItem]:
        return [LookupItem(group=None, pk=item.id, title=item.name, icon=None)
                for item in await self.perform_list()]

    def __init__(self):
        super().__init__(container=database, pk_field="id")

app.include_router(router)
```

This registers the following endpoints automatically:

| Method | Path | Action |
|--------|------|--------|
| `GET` | `/items` | list |
| `POST` | `/items` | create |
| `POST` | `/items/bulk` | bulk_create |
| `GET` | `/items/{pk}` | retrieve |
| `PUT` | `/items/{pk}` | update |
| `PATCH` | `/items/{pk}` | partial_update |
| `PUT` | `/items/bulk` | bulk_update |
| `PATCH` | `/items/bulk` | bulk_partial_update |
| `DELETE` | `/items/{pk}` | destroy |
| `DELETE` | `/items/bulk` | bulk_destroy |
| `GET` | `/items/lookup` | lookup |

### 5. Connect from Vue / TypeScript

```ts
import type { BulkViewSetMixin, LookupMixin } from '@dynamicforms/viewsets';
import { route_rest } from '@dynamicforms/viewsets';

interface Item { id: number; name: string; description: string | null }

const itemsApi = route_rest<BulkViewSetMixin<number, Item, 'id'> & LookupMixin>(
  ItemViewSet,
  '/items',
  'id',
);

const all     = await itemsApi.list();
const one     = await itemsApi.retrieve(1);
const created = await itemsApi.create({ name: 'Widget', description: null });
const lookup  = await itemsApi.lookup();
```
