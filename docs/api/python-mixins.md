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

- `POST /` → calls `perform_create(context, data: T) -> T`. With `pk_field_name` set on `route_viewset`, the body leaves
  the primary key out or gives it as null, and `data` is a `T` without it unless `T` gives it a default.

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
    sort: Annotated[str | None, Query(description=SORT_DESCRIPTION)] = None,
    offset: int = 0,
    limit: int | None = None,
    cursor: str | None = None,
    x_list_shape: Annotated[str | None, Header()] = None,
    x_list_type: Annotated[str | None, Header()] = None,
) -> Union[ListOf[T], PaginatedList[T], CursorPage[T]]: ...
```

- `GET /` → `list_items`, which builds a `ListQuery` out of the parameters and hands it to
  `get_list(context, query)`; `get_list` calls `perform_list(context)` itself, as its first stage.
- `sort` is always exposed (`column:asc,column:desc`, comma-separated). Without it the list is
  ordered by `default_sort`; an empty `sort=` leaves it unordered. When `TFilter` is provided, its
  fields are exposed as individual query parameters; a sequence-typed field is read from a repeated
  key (`?ids=1&ids=2`).
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
- The rows are `T`. A viewset that composes [`MultiTypeListMixin`](#multitypelistmixin-ts) has
  further row types; the `X-List-Type` header (`x_list_type`) selects one by its wire name, and an
  absent header selects the default. `route_viewset` keeps the header only when the list has more
  than one row type, typed as a `Literal` of the wire names plus `None` and described with the
  default's name; any other value is a 422. The return type becomes a `Union` of every declared
  shape of every row type. A direct `list_items` call with a name the list does not have raises
  `UnsupportedListTypeError` (422, `fastapi-viewsets.unsupported_list_type`).

Class attributes:

- `list_shape: str | None = None` — this endpoint's shape, one of `"plain"`, `"paginated"`,
  `"cursor"`. `None` takes `settings.default_list_shape`, which is `"plain"`.
- `list_shapes: tuple[str, ...] | None = None` — shapes a client may ask for with `X-List-Shape`.
  `None` means the default one only.
- `default_page_size: int = 100` — `limit` when a paged shape is asked for without one.
- `max_page_size: int = 1000` — ceiling on `limit`. `limit` is at least 1 and `offset` at least 0; a request outside
  that is a 422.
- `pk_field_name: str = "id"` — appended to the cursor's ordering so every key tuple is unique.
  `DjangoORMViewSet` supplies the model's own primary key instead.
- `default_sort: str | None = None` — the ordering of a request that sends no `sort`, in the `sort`
  parameter's syntax (`"name:asc"`, `"category:asc,score:desc"`). `route_viewset` publishes it as the
  `sort` parameter's default in the schema, which the TS client's `defaultSort()` reads, and raises
  `ValueError` for an empty entry, a direction other than `asc` / `desc`, or a column `T` has
  neither a field nor a computed field for (`check_declared_sort`). It also orders a
  direct `list_items` call without `sort`; `get_list` with a `ListQuery` built by hand takes the
  query's `sort` as given.
- `nulls: str = "first"` — which end NULLs sit at when sorting ascending, `"first"` or `"last"`.
- `nulls_first: bool` — property, `self.nulls != "last"`.
- `resolve_shapes() -> tuple` — classmethod returning `(default shape, shapes allowed)`; the default
  is always first in the allowed tuple.
- `resolve_list_types() -> ListTypes` — classmethod returning the list's row types, resolved once
  per class; see [`MultiTypeListMixin`](#multitypelistmixin-ts). Raises `ValueError` for a declaration
  `MultiTypeListMixin` refuses; `route_viewset` calls it when it decorates the class.

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
  set, and the landed list otherwise. When `query.list_type` is set, the rows of the page are
  converted with `to_list_item` and the envelope carries that type in place of `T`.
- `take_page(query: ListQuery, records: ListRecords) -> tuple[list, bool]` — one page and whether
  anything follows it; the default reads `limit + 1` rows starting at `offset`.
- `count_records(context: Context, query: ListQuery, records: ListRecords) -> int | None` — default
  is `len(records)` for a list or tuple and `None` for anything else, and always `None` for the
  `cursor` shape.
- `to_record(raw: Any) -> T` — synchronous, identity by default. Must tolerate an already-converted
  record.
- `to_list_item(raw: Any, item_type: type) -> Any` — synchronous; converts one row of the page into
  `item_type`, the row type the request selected when it is not `T`. `raw` is a source row when no
  stage has landed the source, otherwise a record. The default returns `raw` when its type is
  exactly `item_type`, and otherwise validates the record — `raw` itself when it is a `T`, else
  `to_record(raw)` — with `validate_list_item(item_type, record)`. `DjangoORMViewSet` builds
  `item_type` straight from the Django row.

```python
def validate_list_item(item_type: type[BaseModel], row: Any) -> Any: ...
```

`fastapi_viewsets.mixins.validate_list_item` validates `row` into `item_type` with
`from_attributes=True`, and returns `row` itself when its type is exactly `item_type`. An instance
of a subclass of `item_type` is validated too, so the result carries `item_type`'s fields only.

In-memory hooks:

- `filter_list(fltr: TFilter, records: list[T]) -> list[T]` — post-filter hook with no default
  implementation; it returns `None`, which `apply_filter` reads as "unfiltered" and answers with the
  records as they stand.
- `sort_list(sort: SortState, records: list[T]) -> list[T]` — post-sort hook. Default is a stable
  multi-key in-memory sort through `compare_in_order`, placing NULLs where `nulls` says. Strings
  compare by code point.

### PaginatedListMixin `[T, TFilter=None]`

`ListMixin` with `list_shape = "paginated"`. `GET /` →
[`PaginatedList[T]`](../guide/list-pipeline#declaring-a-shape), taking `offset` and `limit`.

### CursorListMixin `[T, TFilter=None]`

`ListMixin` with `list_shape = "cursor"`. `GET /` →
[`CursorPage[T]`](../guide/list-pipeline#declaring-a-shape), taking `cursor` and `limit`.

### MultiTypeListMixin `[*Ts]`

```python
from typing import Annotated

from fastapi_viewsets.mixins import MultiTypeListMixin


class MemberViewSet(
    DjangoORMViewSet[int, Member],
    ViewSetMixin[int, Member, MemberFilter],
    MultiTypeListMixin[Annotated[MemberRow, "compact"], MemberCard],
): ...
```

Adds row types to the viewset's list endpoint, `GET /`; a request selects one with the
`X-List-Type` header.

- Each type has a wire name: the first string in its `Annotated` metadata (`compact`), else its
  class name (`MemberCard`).
- With a list mixin composed beside it — `ListMixin`, `PaginatedListMixin`, `CursorListMixin`,
  `ReadOnlyViewSetMixin`, `ViewSetMixin` or `BulkViewSetMixin` — the list's types are `T`, named
  `T.__name__` and the default, followed by the added ones.
- Without one, `MultiTypeListMixin` provides the list endpoint itself: the added types are the only
  ones, the first is the default, and the list has no filter (`TFilter` is `None`).
- The order of the bases does not matter, for `MultiTypeListMixin[...]` itself and for a subclass
  of it composed as a reusable mixin. `MultiTypeListMixin` and `ListMixin` both derive from
  `ListEndpoint`, which holds the endpoint and its pipeline.
- `list_shape` and `list_shapes` apply to every type.
- `route_viewset` raises `ValueError` for `MultiTypeListMixin` without types, a type that is not a
  pydantic model (`list[MemberRow]`, a union, a dataclass) and two types with the same wire name,
  `T`'s included.

Filtering, sorting and the cursor work on records, `T`, whichever type is selected; only the page
that goes into the response is converted into the selected type, by `to_list_item`. The filter
model, `default_sort` and the cursor's keys name fields of `T`, which the selected type need not
carry. A cursor page in another type reads its anchors off its first and last row as records,
converting only those two with `to_record`.

A response is validated as the model of the selected shape and type, over REST, muxws and
`celery_viewset`, also where the rows would validate as another type's.

The response schema of the list operation in the OpenAPI document, and in the viewset's `/schema`,
carries `x-list-types`. Each `$ref` names the component FastAPI generates for the model in that
document (`G_int_` for `G[int]`, a module-qualified name for two models with the same class name):

```json
{
  "default": "Member",
  "types": {
    "Member": {"$ref": "#/components/schemas/Member"},
    "compact": {"$ref": "#/components/schemas/MemberRow"},
    "MemberCard": {"$ref": "#/components/schemas/MemberCard"}
  }
}
```

The response is documented with one example per combination of shape and type, keyed by the
`X-List-Shape` value, the `X-List-Type` value, or `<shape>, <type>` when both vary.

```python
@dataclass(frozen=True)
class ListTypes:
    default: str               # the wire name a request without X-List-Type gets
    types: dict[str, Any]      # wire name to row type, the default first
    record: str | None         # the wire name whose rows are T; None when the list has none
    record_type: Any           # T

    def row_type(self, name: str) -> Any: ...  # ListQuery.list_type for `name`: None for T, else the type
```

`ListQuery.list_type` is the type the page is converted into: `None` for `T`. `list_items` sets it
from the header; `get_list` sets it to the default type when it is `None` and the list has no `T`
entry (no list mixin beside `MultiTypeListMixin`). `fastapi_viewsets.list_shapes.TYPE_HEADER` is `"X-List-Type"`.

### RetrieveMixin `[K, T]`

- `GET /{pk}` → calls `perform_retrieve(context, pk: K) -> T`

### UpdateMixin `[K, T]`

- `PUT /{pk}` → calls `perform_update(context, pk, data, partial=False) -> T`
- `PATCH /{pk}` → calls `perform_update(context, pk, data, partial=True) -> T`. The body has every field of `T` optional
  and no primary key; `data` is a `T` whose `model_fields_set` names the fields the body stated.

### BulkOnlyUpdateMixin `[K, T]`

- `PUT /bulk` → calls `perform_bulk_update(context, records, partial=False) -> list[T]`
- `PATCH /bulk` → calls `perform_bulk_update(context, records, partial=True) -> list[T]`, each record as `PATCH /{pk}`
  passes `data`.

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

`check_declared_sort(sort_csv: str, model: Any = None) -> SortState` — parses a declared
`default_sort` and raises `ValueError` for an empty entry, an unknown direction, and, when `model` is
a pydantic model, a column that is neither a field nor a computed field of it. `route_viewset` calls
it with `T`.

`SORT_DESCRIPTION: str` — the `sort` parameter's description on every list endpoint.

### LookupMixin `[TLookupFilter=LookupFilter, TLookupItem=LookupItem]`

- `GET /lookup` → `get_lookup(context, query: ListQuery) -> list[TLookupItem]`, where `query.fltr`
  is the request's `TLookupFilter`, or `None` when no field of it is set. Its stages:
  - `perform_lookup(context) -> ListRecords` — abstract; the source, lazy or not, ordered by group,
    then by text. The endpoint answers in this order; no stage reorders it.
  - `apply_lookup_filter(context, query, records) -> ListRecords` — runs when `query.has_filter`
    and the `"filter"` stage is not marked applied. The default filters converted items in memory:
    by the declaration of a `make_filter_model` filter model, otherwise with `filter_lookup`.
  - `filter_lookup(fltr: TLookupFilter, items: list[TLookupItem]) -> list[TLookupItem]` — in-memory
    hook of a hand-written filter model; the default keeps items whose `title` contains `fltr.q`,
    ignoring case.
  - `to_lookup_item(raw) -> TLookupItem` — row conversion; identity by default.
  - `land_lookup(query, records) -> list[TLookupItem]` — materialises the source and converts it,
    once per request.
- The response model is `list[TLookupItem]`.

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
# HTTP 404: {"detail": "Item with pk <pk> not found", "detail_code": "fastapi-viewsets.not_found", "detail_params": {"pk": <pk>}, "field_errors": {}}
```
