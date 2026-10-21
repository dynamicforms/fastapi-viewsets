# Python Mixins

The mixin system is the core of `fastapi-viewsets`. Each mixin adds one or more HTTP endpoints to a viewset class. You
compose viewsets by inheriting from the mixins you need.

## How it works

Every mixin class owns a private `APIRouter` and registers its endpoints on it using standard FastAPI decorators. The
`route_viewset` decorator later collects all routes from the class hierarchy, resolves generic type parameters, and
registers them on your application router.

You implement the actual business logic by overriding the corresponding `perform_*` method from `ImplMixin`. Every
`perform_*` method takes a `context: Context` as its first parameter after `self` - see [Architecture](./architecture)
for the request pipeline that builds it, and [Context Processors](./context-processors) for how to use it.

## Individual operation mixins

### Create

| Class | Endpoint | Description |
|-------|----------|-------------|
| `CreateMixin[K, T]` | `POST /` | Create a single record |
| `BulkOnlyCreateMixin[K, T]` | `POST /bulk` | Create multiple records |
| `BulkCreateMixin[K, T]` | both above | Single + bulk create |

```python
from fastapi_viewsets.context import Context
from fastapi_viewsets.mixins import BulkCreateMixin

class MyViewSet(BulkCreateMixin[int, Item]):
    async def perform_create(self, context: Context, data: Item) -> Item:
        db.append(data)
        return data

    async def perform_bulk_create(self, context: Context, data: list[Item]) -> list[Item]:
        db.extend(data)
        return data
```

With `pk_field_name` set on `route_viewset`, the `POST /` body carries no primary key, and every
other field is validated as `Item` declares it. `perform_create` receives an `Item` without the
primary key unless `Item` gives it a default. See
[pk_field_name and request body](./routers#pk-field-name-and-request-body).

### List & Retrieve

| Class | Endpoint | Description |
|-------|----------|-------------|
| `ListMixin[T, TFilter]` | `GET /` | Return all records, with optional filter and sort |
| `RetrieveMixin[K, T]` | `GET /{pk}` | Return a single record by PK |

`ListMixin` is generic over an optional `TFilter` type (default: `None`). The endpoint always
exposes a `sort` query parameter (comma-separated `column:direction` pairs). When a filter model
is also provided, its fields appear as individual query parameters.

**Filtering** (active when any filter field is set): `filter_list(fltr, records)` filters in
memory. It has no default implementation, so a viewset that declares a filter type implements it —
or declares the filter with `make_filter_model`, which needs no code at all. See
[Declarative filters](./list-pipeline.md#declarative-filters).

**Sorting** (active when the `sort` query parameter is non-empty, or is absent and the viewset
declares `default_sort`): `sort_list(sort, records)` sorts in memory. The default is a stable
multi-key sort; NULLs sit where the viewset's `nulls` says, lowest in both directions by default.

To do either in the backend instead, override `apply_filter` or `apply_sort` — see
[the list pipeline](./list-pipeline.md#the-stages).

```python
from fastapi_viewsets.context import Context
from fastapi_viewsets.mixins import ListMixin, RetrieveMixin, make_all_optional, SortState
from fastapi_viewsets.exceptions import NotFoundError

ItemFilter = make_all_optional(Item)

class MyViewSet(ListMixin[Item, ItemFilter], RetrieveMixin[int, Item]):
    async def perform_list(self, context: Context) -> list[Item]:
        return list(db.values())

    async def filter_list(self, fltr: ItemFilter, records: list[Item]) -> list[Item]:
        # filter_list does not receive context - see Context Processors "Known limitations"
        if fltr.name is not None:
            records = [r for r in records if fltr.name.lower() in r.name.lower()]
        return records

    async def perform_retrieve(self, context: Context, pk: int) -> Item:
        if pk not in db:
            raise NotFoundError(pk)
        return db[pk]
```

**Sorting** works out of the box without any overrides:

```text
GET /items?sort=name:asc,score:desc
```

`default_sort` orders a request that sends no `sort`. It is published as the `sort` parameter's
default in `/schema`, where the TS client reads it with `defaultSort()`, e.g. as a grid's initial
`sortState`. An empty `sort=` leaves the list unordered:

```python
class CountryViewSet(CollectionViewSet[int, Country], ListMixin[Country, None]):
    default_sort = "name:asc"
```

For server-side sorting, override `apply_sort`, order the records in the backend and mark the stage
applied so the in-memory sort leaves them alone:

```python
async def apply_sort(self, context, query, records):
    if query.has_sort:
        records = records.order_by(*(column.column_name for column in query.sort))
        query.mark_applied("sort")
    return await super().apply_sort(context, query, records)
```

### Update

| Class | Endpoint | Description |
|-------|----------|-------------|
| `UpdateMixin[K, T]` | `PUT /{pk}`, `PATCH /{pk}` | Full and partial update |
| `BulkOnlyUpdateMixin[K, T]` | `PUT /bulk`, `PATCH /bulk` | Bulk full and partial update |
| `BulkUpdateMixin[K, T]` | all four above | Single + bulk update |

The `partial` parameter is `False` for `PUT` and `True` for `PATCH`.

A `PUT` body is the whole model. A `PATCH` body states the fields it changes: every field is
optional, the primary key is not part of it, and a field it states is validated by its annotation,
its constraints and the model's `@field_validator`s. The model's `@model_validator`s are not
applied: they validate a whole model, and a partial body is not one. `DjangoORMViewSet` applies
them to the stored row with the stated fields set on it
([Backends](./list-pipeline#updates-under-a-row-lock)). `perform_update` receives it
as the model holding the stated fields only: `data.model_fields_set` names them, and
`data.model_dump(exclude_unset=True)` is what the request stated; `PATCH /bulk` does the same for
each record. A field read with `getattr` keeps a nested model as the model; `model_dump` turns it
into a `dict`.

```python
async def perform_update(self, context: Context, pk: int, data: Item, partial: bool = True) -> Item:
    if partial:
        db[pk] = db[pk].model_copy(update={name: getattr(data, name) for name in data.model_fields_set})
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

`LookupMixin[TLookupFilter, TLookupItem]` adds a `GET /lookup` endpoint that returns a list of
`TLookupItem` objects — useful for populating select/autocomplete widgets. `TLookupFilter` defaults
to `LookupFilter` (a single `q: str | None` field) and `TLookupItem` to `LookupItem`
(`group`, `pk`, `title`, `icon`). The fields of `TLookupFilter` are the endpoint's query parameters.

A lookup request runs through the same stages as a [list](./list-pipeline):

1. **`perform_lookup(context)`** returns the source: a list, a generator or an unevaluated QuerySet.
2. **`apply_lookup_filter(context, query, records)`** narrows it when any filter field is set. The
   filter is `query.fltr`. Override the stage to filter in the backend and call
   `query.mark_applied("filter")`; the default then leaves the records as they are. Without an
   override, the default filters the converted items in memory: by the declaration of a filter model
   built with `make_filter_model`, otherwise through **`filter_lookup(fltr, items)`**, whose default
   keeps the items whose `title` contains `fltr.q`, ignoring case.
3. **`to_lookup_item(raw)`** converts each row to `TLookupItem`. The default is identity.

The filter is passed through `query`, so concurrent requests on one viewset instance (the default
`singleton` lifecycle) do not share it.

```python
from fastapi_viewsets.mixins import LookupMixin, LookupItem, LookupFilter

# Default behaviour: q= query param, filters by title
class MyViewSet(..., LookupMixin):
    async def perform_lookup(self, context: Context) -> list[LookupItem]:
        return [LookupItem(group=None, pk=i.id, title=i.name, icon=None) for i in db.values()]
```

A lookup with its own item type, `q` and `ids` parameters, filtered in the database:

```python
class TagLookup(BaseModel):
    id: int
    name: str
    color: str

class TagLookupFilter(BaseModel):
    q: str | None = None
    ids: list[int] | None = None  # ?ids=1&ids=2

class TagViewSet(..., LookupMixin[TagLookupFilter, TagLookup]):
    async def perform_lookup(self, context: Context):
        return Tag.objects.order_by("name")

    async def apply_lookup_filter(self, context, query, records):
        if query.has_filter:
            if query.fltr.q:
                records = records.filter(name__icontains=query.fltr.q)
            if query.fltr.ids:
                records = records.filter(id__in=query.fltr.ids)
            query.mark_applied("filter")
        return await super().apply_lookup_filter(context, query, records)

    def to_lookup_item(self, raw: Tag) -> TagLookup:
        return TagLookup(id=raw.id, name=raw.name, color=raw.color)
```

A filter model built with `make_filter_model(TLookupItem, {...})` is applied without code, in
memory. Its operators compare values of the field's declared type, so `LookupItem.pk`, declared as
`object`, takes no typed comparison: `pk__in=1,2` compares strings.

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
    async def perform_list(self, context): ...
    async def perform_retrieve(self, context, pk): ...
    async def perform_create(self, context, data): ...
    async def perform_bulk_create(self, context, data): ...
    async def perform_update(self, context, pk, data, partial): ...
    async def perform_bulk_update(self, context, records, partial): ...
    async def perform_destroy(self, context, pk): ...
    async def perform_bulk_destroy(self, context, pk): ...
    async def filter_list(self, fltr, records): ...  # not context-threaded, see Context Processors
```

## Error handling

Raise `NotFoundError` when a record does not exist. It produces a proper `404` response with a JSON body.

```python
from fastapi_viewsets.exceptions import NotFoundError

raise NotFoundError(pk)
# → HTTP 404: {"detail": "Item with pk 42 not found", "detail_code": "fastapi-viewsets.not_found", "detail_params": {"pk": 42}, "field_errors": {}}
```
