# Python Mixins

The mixin system is the core of `fastapi-viewsets`. Each mixin adds one or more HTTP endpoints to a viewset class. You compose viewsets by inheriting from the mixins you need.

## How it works

Every mixin class owns a private `APIRouter` and registers its endpoints on it using standard FastAPI decorators. The `route_viewset` decorator later collects all routes from the class hierarchy, resolves generic type parameters, and registers them on your application router.

You implement the actual business logic by overriding the corresponding `perform_*` method from `ImplMixin`.

## Individual operation mixins

### Create

| Class | Endpoint | Description |
|-------|----------|-------------|
| `CreateMixin[K, T]` | `POST /` | Create a single record |
| `BulkOnlyCreateMixin[K, T]` | `POST /bulk` | Create multiple records |
| `BulkCreateMixin[K, T]` | both above | Single + bulk create |

```python
from fastapi_viewsets.mixins import BulkCreateMixin

class MyViewSet(BulkCreateMixin[int, Item]):
    async def perform_create(self, data: Item) -> Item:
        db.append(data)
        return data

    async def perform_bulk_create(self, data: list[Item]) -> list[Item]:
        db.extend(data)
        return data
```

### List & Retrieve

| Class | Endpoint | Description |
|-------|----------|-------------|
| `ListMixin[T, TFilter]` | `GET /` | Return all records, with optional filter and sort |
| `RetrieveMixin[K, T]` | `GET /{pk}` | Return a single record by PK |

`ListMixin` is generic over an optional `TFilter` type (default: `None`). The endpoint always
exposes a `sort` query parameter (comma-separated `column:direction` pairs). When a filter model
is also provided, its fields appear as individual query parameters.

**Filter pipeline** (active when any filter field is set):
1. `setup_filter(fltr)` — pre-filter hook (e.g., build a DB query). No-op by default.
2. `filter_list(fltr, records)` — post-filter hook for in-memory filtering. No default
   implementation; subclasses must implement this.

**Sort pipeline** (active when `sort` query param is provided):
1. `setup_sort(sort)` — pre-sort hook (e.g., add ORDER BY to a DB query). No-op by default.
2. `sort_list(sort, records)` — post-sort hook. Default implements stable in-memory multi-key
   sort with nulls-last (asc) / nulls-first (desc).

```python
from fastapi_viewsets.mixins import ListMixin, RetrieveMixin, make_all_optional, SortState
from fastapi_viewsets.response_classes import NotFoundError

ItemFilter = make_all_optional(Item)

class MyViewSet(ListMixin[Item, ItemFilter], RetrieveMixin[int, Item]):
    async def perform_list(self) -> list[Item]:
        return list(db.values())

    async def filter_list(self, fltr: ItemFilter, records: list[Item]) -> list[Item]:
        if fltr.name is not None:
            records = [r for r in records if fltr.name.lower() in r.name.lower()]
        return records

    async def perform_retrieve(self, pk: int) -> Item:
        if pk not in db:
            raise NotFoundError(pk)
        return db[pk]
```

**Sorting** works out of the box without any overrides:

```
GET /items?sort=name:asc,score:desc
```

For server-side sorting (e.g. SQL ORDER BY), override `setup_sort` and return pre-sorted results
from `perform_list` (then override `sort_list` to be a no-op):

```python
async def setup_sort(self, sort: SortState) -> None:
    # store sort state for use in perform_list
    self._sort = sort

async def sort_list(self, sort: SortState, records: list[Item]) -> list[Item]:
    return records  # already sorted by perform_list
```

### Update

| Class | Endpoint | Description |
|-------|----------|-------------|
| `UpdateMixin[K, T]` | `PUT /{pk}`, `PATCH /{pk}` | Full and partial update |
| `BulkOnlyUpdateMixin[K, T]` | `PUT /bulk`, `PATCH /bulk` | Bulk full and partial update |
| `BulkUpdateMixin[K, T]` | all four above | Single + bulk update |

The `partial` parameter is `False` for `PUT` and `True` for `PATCH`.

```python
async def perform_update(self, pk: int, data: Item, partial: bool = True) -> Item:
    if partial:
        # apply only provided fields
        ...
    else:
        db[pk] = data
    return db[pk]
```

### Destroy

| Class | Endpoint | Description |
|-------|----------|-------------|
| `DestroyMixin[K, T]` | `DELETE /{pk}` | Delete a single record |
| `BulkOnlyDestroyMixin[K, T]` | `DELETE /bulk` | Delete multiple records |
| `BulkDestroyMixin[K, T]` | both above | Single + bulk delete |

`perform_destroy` should return a dict with the deleted PK as key (and any extra info as value).

### Lookup

`LookupMixin[TLookupFilter]` adds a `GET /lookup` endpoint that returns a list of `LookupItem`
objects — useful for populating select/autocomplete widgets.

The mixin is generic over an optional `TLookupFilter` type (default: `LookupFilter` with a single
`q: str | None` field). When the filter is active, a two-phase pipeline runs:

1. **`setup_lookup_filter(fltr)`** — pre-filter hook (e.g., build a DB query). No-op by default.
2. **`filter_lookup(fltr, items)`** — post-filter hook. The **default implementation** filters by
   `fltr.q` (case-insensitive substring match on `title`), so basic search works out of the box.

```python
from fastapi_viewsets.mixins import LookupMixin, LookupItem, LookupFilter

# Default behaviour: q= query param, filters by title automatically
class MyViewSet(..., LookupMixin):
    async def perform_lookup(self) -> list[LookupItem]:
        return [LookupItem(group=None, pk=i.id, title=i.name, icon=None) for i in db.values()]

# Custom filter model with additional fields
class MyFilter(LookupFilter):
    group: str | None = None

class MyViewSet(..., LookupMixin[MyFilter]):
    async def perform_lookup(self) -> list[LookupItem]:
        return [LookupItem(group=i.group, pk=i.id, title=i.name) for i in db.values()]

    async def filter_lookup(self, fltr: MyFilter, items: list[LookupItem]) -> list[LookupItem]:
        if fltr.q is not None:
            items = [i for i in items if fltr.q.lower() in i.title.lower()]
        if fltr.group is not None:
            items = [i for i in items if i.group == fltr.group]
        return items
```

## Combined viewset mixins

For convenience, three pre-composed classes are provided:

| Class | Included actions |
|-------|-----------------|
| `ReadOnlyViewSetMixin[K, T]` | `list`, `retrieve` |
| `ViewSetMixin[K, T, TFilter=None]` | `list`, `retrieve`, `create`, `update`, `partial_update`, `destroy` |
| `BulkViewSetMixin[K, T, TFilter=None]` | all of the above + `bulk_create`, `bulk_update`, `bulk_partial_update`, `bulk_destroy` |

```python
from fastapi_viewsets.mixins import BulkViewSetMixin, make_all_optional

ItemFilter = make_all_optional(Item)

class ItemViewSet(BulkViewSetMixin[int, Item, ItemFilter]):
    async def perform_list(self): ...
    async def perform_retrieve(self, pk): ...
    async def perform_create(self, data): ...
    async def perform_bulk_create(self, data): ...
    async def perform_update(self, pk, data, partial): ...
    async def perform_bulk_update(self, records, partial): ...
    async def perform_destroy(self, pk): ...
    async def perform_bulk_destroy(self, pk): ...
    async def filter_list(self, fltr, records): ...
```

## Error handling

Raise `NotFoundError` when a record does not exist. It produces a proper `404` response with a JSON body.

```python
from fastapi_viewsets.response_classes import NotFoundError

raise NotFoundError(pk)
# → HTTP 404: {"detail": "Item with pk 42 not found"}
```
