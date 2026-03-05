# Python Mixins — API Reference

## ImplMixin

Abstract base. Subclasses must implement all `perform_*` methods.

```python
class ImplMixin(Generic[K, T], ABC):
    async def perform_create(self, data: T) -> T: ...
    async def perform_bulk_create(self, data: list[T]) -> list[T]: ...
    async def perform_list(self) -> list[T]: ...
    async def perform_retrieve(self, pk: K) -> T: ...
    async def perform_update(self, pk: K, data: T, partial: bool = True) -> T: ...
    async def perform_bulk_update(self, records: dict[K, T], partial: bool = True) -> list[T]: ...
    async def perform_destroy(self, pk: K) -> dict[K, Any]: ...
    async def perform_bulk_destroy(self, pk: list[K]) -> list[dict[K, Any]]: ...
```

## Operation mixins

### CreateMixin `[K, T]`
- `POST /` → calls `perform_create(data: T) -> T`

### BulkOnlyCreateMixin `[K, T]`
- `POST /bulk` → calls `perform_bulk_create(data: list[T]) -> list[T]`

### BulkCreateMixin `[K, T]`
Combines `CreateMixin` + `BulkOnlyCreateMixin`.

### ListMixin `[T, TFilter=None]`
- `GET /` → calls `perform_list() -> list[T]`
- Always exposes a `sort` query parameter (`column:asc,column:desc` comma-separated format).
- When `TFilter` is provided, filter fields are also exposed as individual query parameters.

Filter hooks:
- `setup_filter(fltr: TFilter)` — pre-filter hook (no-op by default)
- `filter_list(fltr: TFilter, records: list[T]) -> list[T]` — post-filter hook (no default; subclasses must implement)

Sort hooks:
- `setup_sort(sort: SortState)` — pre-sort hook (no-op by default)
- `sort_list(sort: SortState, records: list[T]) -> list[T]` — post-sort hook (default: stable multi-key in-memory sort; nulls last for asc, nulls first for desc)

### RetrieveMixin `[K, T]`
- `GET /{pk}` → calls `perform_retrieve(pk: K) -> T`

### UpdateMixin `[K, T]`
- `PUT /{pk}` → calls `perform_update(pk, data, partial=False) -> T`
- `PATCH /{pk}` → calls `perform_update(pk, data, partial=True) -> T`

### BulkOnlyUpdateMixin `[K, T]`
- `PUT /bulk` → calls `perform_bulk_update(records, partial=False) -> list[T]`
- `PATCH /bulk` → calls `perform_bulk_update(records, partial=True) -> list[T]`

### BulkUpdateMixin `[K, T]`
Combines `UpdateMixin` + `BulkOnlyUpdateMixin`.

### DestroyMixin `[K, T]`
- `DELETE /{pk}` → calls `perform_destroy(pk: K) -> dict[K, Any]`

### BulkOnlyDestroyMixin `[K, T]`
- `DELETE /bulk` → calls `perform_bulk_destroy(pk: list[K]) -> list[dict[K, Any]]`

### BulkDestroyMixin `[K, T]`
Combines `DestroyMixin` + `BulkOnlyDestroyMixin`.

### Sort types

```python
class SortDirection(str, Enum):
    asc = "asc"
    desc = "desc"

class SortStateColumn(BaseModel):
    column_name: str   # serialises as columnName (camelCase) via model_dump(by_alias=True)
    direction: SortDirection = SortDirection.asc

SortState = list[SortStateColumn]
```

`parse_sort_param(sort_csv: str | None) -> SortState` — parses the `sort` query string into a
`SortState`. Format: `"name:asc,score:desc"`. Entries with an unknown direction are skipped.

### LookupMixin `[TLookupFilter=LookupFilter]`
- `GET /lookup` → calls `perform_lookup() -> list[LookupItem]`
- `TLookupFilter` defaults to `LookupFilter` (single `q: str | None` field), so basic title search
  works without any configuration.
- When the filter is active:
  - `setup_lookup_filter(fltr: TLookupFilter)` — pre-filter hook (no-op by default)
  - `filter_lookup(fltr: TLookupFilter, items: list[LookupItem]) -> list[LookupItem]` — post-filter hook;
    default filters by `fltr.q` (case-insensitive substring of `title`)

```python
class LookupItem(BaseModel):
    group: Any
    pk: object
    title: str
    icon: str | None

class LookupFilter(BaseModel):
    q: str | None = None
```

## Combined viewset mixins

### ReadOnlyViewSetMixin `[K, T]`
Inherits: `ListMixin[T]`, `RetrieveMixin[K, T]`

### ViewSetMixin `[K, T]`
Inherits: `CreateMixin`, `ListMixin`, `RetrieveMixin`, `UpdateMixin`, `DestroyMixin`

### BulkViewSetMixin `[K, T]`
Inherits: `BulkCreateMixin`, `ListMixin`, `RetrieveMixin`, `BulkUpdateMixin`, `BulkDestroyMixin`

## NotFoundError

```python
from fastapi_viewsets.response_classes import NotFoundError

raise NotFoundError(pk)
# HTTP 404: {"detail": "Item with pk <pk> not found"}
```
