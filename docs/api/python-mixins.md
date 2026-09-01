# Python Mixins — API Reference

## ImplMixin

Abstract base. Subclasses must implement all `perform_*` methods.

```python
class ImplMixin(Generic[K, T], ABC):
    async def perform_create(self, context: Context, data: T) -> T: ...
    async def perform_bulk_create(self, context: Context, data: list[T]) -> list[T]: ...
    async def perform_list(self, context: Context) -> ListRecords: ...
    async def perform_retrieve(self, context: Context, pk: K) -> T: ...
    async def perform_update(self, context: Context, pk: K, data: T, partial: bool = True) -> T: ...
    async def perform_bulk_update(self, context: Context, records: dict[K, T], partial: bool = True) -> list[T]: ...
    async def perform_destroy(self, context: Context, pk: K) -> dict[K, Any]: ...
    async def perform_bulk_destroy(self, context: Context, pk: list[K]) -> list[dict[K, Any]]: ...
```

`context` (a [`Context`](../guide/context-processors) instance) is built by `route_viewset` from
`settings.viewsets_context_processors` - see [Architecture](../guide/architecture).

```python
from fastapi_viewsets.list_query import ListRecords
```

`ListRecords` is `Iterable[TItem] | AsyncIterable[TItem]`. Written bare — as `perform_list` and every
pipeline stage write it — it means `ListRecords[Any]`: what a backend yields is its own row type, and
records are `T` only once `to_record` has run over them.

The same module carries the two helpers the pipeline reads a source with.
`materialize(records: ListRecords[TItem]) -> list[TItem]` drains a lazy source when an in-memory
stage needs a list and returns a list untouched;
`take_page(records: ListRecords[TItem], offset: int, limit: int) -> tuple[list[TItem], bool]` walks a
lazy source only as far as the page it is cutting. Both hand back the item type they were given. The
`ListMixin.take_page` hook below is the overridable stage of the same name and delegates to this one.

## Operation mixins

### CreateMixin `[K, T]`
- `POST /` → calls `perform_create(context, data: T) -> T`

### BulkOnlyCreateMixin `[K, T]`
- `POST /bulk` → calls `perform_bulk_create(context, data: list[T]) -> list[T]`

### BulkCreateMixin `[K, T]`
Combines `CreateMixin` + `BulkOnlyCreateMixin`.

### ListMixin `[T, TFilter=None]`

```python
async def list_items(
    self,
    context: Context,
    fltr: Annotated[TFilter, Query()] = None,
    sort: str | None = None,
    offset: int = 0,
    limit: int | None = None,
    cursor: str | None = None,
    x_list_shape: Annotated[str | None, Header()] = None,
) -> Union[ListOf[T], PaginatedList[T], CursorPage[T]]: ...
```

- `GET /` → `list_items`, which builds a `ListQuery` out of the parameters and hands it to
  `get_list(context, query)`; `get_list` calls `perform_list(context)` itself, as its first stage.
- `sort` is always exposed (`column:asc,column:desc`, comma-separated). When `TFilter` is provided,
  its fields are exposed as individual query parameters.
- `route_viewset` narrows both the parameters and the return type per viewset: `offset` survives
  only when `paginated` is among the declared shapes, `cursor` only when `cursor` is, `limit` when
  either is, and the `X-List-Shape` header only when more than one shape is declared. The return
  type becomes the single declared shape's model, or a `Union` of exactly the declared ones.
- The `plain` shape discards `limit` and answers unpaginated; `cursor` is read only by the `cursor`
  shape.
- Where the header survives, it is typed as a `Literal` of the declared shapes plus `None`, so any
  other value is a 422 and never reaches the handler. Where it does not, a header sent anyway is
  ignored, whatever it names, and the endpoint answers in its one declared shape.
- A cursor that does not decode against the current ordering and filters is a 400.

Class attributes:
- `list_shape: str | None = None` — this endpoint's shape, one of `"plain"`, `"paginated"`,
  `"cursor"`. `None` takes `settings.default_list_shape`, which is `"plain"`.
- `list_shapes: tuple[str, ...] | None = None` — shapes a client may ask for with `X-List-Shape`.
  `None` means the default one only.
- `default_page_size: int = 100` — `limit` when a paged shape is asked for without one.
- `max_page_size: int = 1000` — ceiling on `limit`.
- `pk_field_name: str = "id"` — appended to the cursor's ordering so every key tuple is unique.
  `DjangoORMViewSet` supplies the model's own primary key instead.
- `nulls: str = "first"` — which end NULLs sit at when sorting ascending, `"first"` or `"last"`.
- `nulls_first: bool` — property, `self.nulls != "last"`.
- `resolve_shapes() -> tuple` — classmethod returning `(default shape, shapes allowed)`; the default
  is always first in the allowed tuple.

Pipeline hooks, overridable one at a time — see [the list pipeline](../guide/list-pipeline) for
what pushing a stage into a backend involves:
- `get_list(context: Context, query: ListQuery) -> Any` — the pipeline as a whole:
  `perform_list` → `apply_filter` → `apply_sort` → `apply_pagination`.
- `apply_filter(context: Context, query: ListQuery, records: ListRecords) -> ListRecords` — default
  applies the declared filters plus the cursor predicate, and delegates a hand-written filter model
  to `filter_list`.
- `apply_sort(context: Context, query: ListQuery, records: ListRecords) -> ListRecords` — default
  delegates to `sort_list`.
- `land(query: ListQuery, records: ListRecords) -> list` — materialises the source and converts it
  with `to_record`. Idempotent; every in-memory stage goes through it.
- `apply_pagination(context: Context, query: ListQuery, records: ListRecords) -> list[T] | PaginatedList[T]`
  — returns a `CursorPage[T]` for the `cursor` shape, a `PaginatedList[T]` when `query.limit` is
  set, and the landed list otherwise.
- `take_page(query: ListQuery, records: ListRecords) -> tuple[list, bool]` — one page and whether
  anything follows it; the default reads `limit + 1` rows starting at `offset`.
- `count_records(context: Context, query: ListQuery, records: ListRecords) -> int | None` — default
  is `len(records)` for a list or tuple and `None` for anything else, and always `None` for the
  `cursor` shape.
- `to_record(raw: Any) -> T` — synchronous, identity by default. Must tolerate an already-converted
  record.

In-memory hooks:
- `filter_list(fltr: TFilter, records: list[T]) -> list[T]` — post-filter hook with no default
  implementation; it returns `None`, which `apply_filter` reads as "unfiltered" and answers with the
  records as they stand.
- `sort_list(sort: SortState, records: list[T]) -> list[T]` — post-sort hook. Default is a stable
  multi-key in-memory sort through `compare_in_order`, placing NULLs where `nulls` says.

### PaginatedListMixin `[T, TFilter=None]`
`ListMixin` with `list_shape = "paginated"`. `GET /` →
[`PaginatedList[T]`](../guide/list-pipeline#declaring-a-shape), taking `offset` and `limit`.

### CursorListMixin `[T, TFilter=None]`
`ListMixin` with `list_shape = "cursor"`. `GET /` →
[`CursorPage[T]`](../guide/list-pipeline#declaring-a-shape), taking `cursor` and `limit`.

### RetrieveMixin `[K, T]`
- `GET /{pk}` → calls `perform_retrieve(context, pk: K) -> T`

### UpdateMixin `[K, T]`
- `PUT /{pk}` → calls `perform_update(context, pk, data, partial=False) -> T`
- `PATCH /{pk}` → calls `perform_update(context, pk, data, partial=True) -> T`

### BulkOnlyUpdateMixin `[K, T]`
- `PUT /bulk` → calls `perform_bulk_update(context, records, partial=False) -> list[T]`
- `PATCH /bulk` → calls `perform_bulk_update(context, records, partial=True) -> list[T]`

### BulkUpdateMixin `[K, T]`
Combines `UpdateMixin` + `BulkOnlyUpdateMixin`.

### DestroyMixin `[K, T]`
- `DELETE /{pk}` → calls `perform_destroy(context, pk: K) -> dict[K, Any]`

### BulkOnlyDestroyMixin `[K, T]`
- `DELETE /bulk` → calls `perform_bulk_destroy(context, pk: list[K]) -> list[dict[K, Any]]`

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
`SortState`. Format: `"name:asc,score:desc"`; a bare column name means ascending. Entries with an
unknown direction are skipped.

### LookupMixin `[TLookupFilter=LookupFilter]`
- `GET /lookup` → calls `perform_lookup(context) -> list[LookupItem]`
- `TLookupFilter` defaults to `LookupFilter` (single `q: str | None` field), so basic title search
  works without any configuration.
- When the filter is active — any field of the filter model is non-`None`:
  - `setup_lookup_filter(fltr: TLookupFilter)` — pre-filter hook (no-op by default)
  - `filter_lookup(fltr: TLookupFilter, items: list[LookupItem]) -> list[LookupItem]` — post-filter hook;
    default filters by `fltr.q` (case-insensitive substring of `title`)

```python
class LookupItem(BaseModel):
    group: Any = None
    pk: object
    title: str
    icon: str | None = None

class LookupFilter(BaseModel):
    q: str | None = None
```

## Combined viewset mixins

### ReadOnlyViewSetMixin `[K, T]`
Inherits: `ListMixin[T]`, `RetrieveMixin[K, T]`

### ViewSetMixin `[K, T, TFilter=None]`
Inherits: `CreateMixin`, `ListMixin`, `RetrieveMixin`, `UpdateMixin`, `DestroyMixin`

### BulkViewSetMixin `[K, T, TFilter=None]`
Inherits: `BulkCreateMixin`, `ListMixin`, `RetrieveMixin`, `BulkUpdateMixin`, `BulkDestroyMixin`

## NotFoundError

```python
from fastapi_viewsets.exceptions import NotFoundError

raise NotFoundError(pk)
# HTTP 404: {"detail": "Item with pk <pk> not found"}
```
