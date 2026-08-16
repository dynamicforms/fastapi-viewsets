# The list pipeline

`GET /{base_path}` runs the records through four steps, each of which a viewset can replace:

```
perform_list  →  apply_filter  →  apply_sort  →  apply_pagination
```

`get_list` is the whole pipeline in one method; override it to rearrange the steps rather than
replace one of them.

## perform_list may be lazy

`perform_list` returns any iterable or async iterable — a list, a generator, an async generator, or
whatever cursor object a database driver hands back:

```python
class TrackViewSet(ListMixin[Track, TrackFilter]):
    async def perform_list(self, context: Context):
        async def rows():
            async for row in self.db.stream("SELECT * FROM tracks"):
                yield Track(**row)
        return rows()
```

A plain list is equally valid, and is what `CollectionViewSet` returns.

## The stages

Each stage receives the records and the `ListQuery` describing what was asked for, and returns
records. Override one and chain the rest with `super()`:

```python
async def apply_sort(self, context, query: ListQuery, records: ListRecords) -> ListRecords:
    if query.has_sort:
        records = self.db.order_by(*(column.column_name for column in query.sort))
        query.mark_applied("sort")
    return await super().apply_sort(context, query, records)
```

`query.mark_applied("sort")` tells the in-memory default that this stage is done, so it leaves the
rows alone. Each stage is marked independently: push down the sort, leave the filter to the
default, and neither needs to know about the other.

Stage names are `"filter"`, `"sort"` and `"pagination"`.

`filter_list` and `sort_list` remain the place to implement in-memory filtering and sorting; the
default `apply_*` stages call them.

### ListQuery

| field | meaning |
|---|---|
| `fltr` | the filter model, or None when the client sent no filter |
| `sort` | list of `SortStateColumn`, in priority order |
| `offset` / `limit` | the requested page; `limit` is None when not paginated |
| `cursor` | the opaque cursor, when the shape is `cursor` |
| `shape` | which envelope the answer goes into |
| `applied` | stages a backend has already handled |

A filter model whose every field is None counts as *no filter*: FastAPI builds the model whether or
not the client sent anything in it, so a null check cannot answer the question.

## Declaring a shape

A list endpoint answers in one of three shapes:

| shape | response | suits | cannot |
|---|---|---|---|
| `plain` | a bare array | a lookup table | scale |
| `paginated` | `{results, offset, limit, count, has_more, has_previous}` | jumping to page 40 | avoid re-reading the 39 in front, or drifting when rows are inserted |
| `cursor` | `{results, next, previous, first, last, has_more, has_previous}` | walking a live list | jump to a page, or report a total |

The viewset says which:

```python
class TrackViewSet(CollectionViewSet[int, Track], ListMixin[Track, TrackFilter]):
    list_shape = "cursor"
    default_page_size = 50
    max_page_size = 500
```

`PaginatedListMixin` and `CursorListMixin` are shorthands for `list_shape = "paginated"` and
`"cursor"`. Unset, the shape comes from `settings.default_list_shape`, which is `"plain"`.

The declaration decides the endpoint's parameters and its response model:

| `list_shape` | parameters | response |
|---|---|---|
| `plain` | `fltr`, `sort` | `ListOf_Track_` |
| `paginated` | `+ offset, limit` | `PaginatedList_Track_` |
| `cursor` | `+ cursor, limit` | `CursorPage_Track_` |

`limit` is capped at `max_page_size`.

### Letting the client choose

Add `list_shapes` and a client may request any of them with an `X-List-Shape` header:

```python
class TrackViewSet(CollectionViewSet[int, Track], ListMixin[Track, TrackFilter]):
    list_shape = "cursor"                            # the default
    list_shapes = ("cursor", "plain", "paginated")   # what a client may request
```

The endpoint gains the header and every parameter those shapes use, and its response becomes a
union of exactly those models. Each is documented with an example named after the header value that
produces it, so the dropdown reads `plain` rather than `ListOf_Track_`. A value not in
`list_shapes` is a 422.

Declare one shape unless clients genuinely differ. A union has to be narrowed by every generated
client, and OpenAPI cannot express that a request header selects between its members — that part is
prose in the endpoint's description.

### On the client

A ViewSet declares the mixin matching the shape its endpoint answers in - see
[the ViewSet factory](../api/viewset-factory.md). Each of the three reads the same `GET {basePath}`:

```ts
import { restViewSet, CursorListMixin } from '@dynamicforms/fastapi-viewsets';

interface Track { id: number; title: string; year: number }

class TrackViewSet extends restViewSet<Track>()('id', [CursorListMixin]) {}

const tracks = new TrackViewSet({ basePath: '/tracks' });
let next = await tracks.listCursor({ limit: 50 });
```

`list()` and `listPage()` are the same call against a viewset whose default is `plain` or
`paginated`. The client sends no `X-List-Shape` header, so a viewset offering several shapes still
answers this client in its default one.

The envelope's `snake_case` fields are renamed to `hasMore`, `hasPrevious`; the records inside are
untouched.

## Paging behaviour

Both paged shapes read `limit + 1` rows; the extra one answers `has_more`. A page out of a
million-row generator costs `offset + limit + 1` steps.

`count` is null when the source cannot be counted without draining it. A backend that can count
cheaply overrides `count_records()`. A cursor page never reports a total.

## Cursor pagination

`CursorListMixin` — or `list_shape = "cursor"` — pages by position:

```python
class TrackViewSet(CollectionViewSet[int, Track], CursorListMixin[Track, TrackFilter]):
    schema = Track          # the response model, used to coerce cursor values back from JSON
    pk_field_name = "id"    # what makes every key tuple unique; the default
    default_page_size = 50
```

The endpoint takes `cursor` and `limit`, and answers:

```json
{
  "results": [...],
  "limit": 50,
  "has_more": true,
  "has_previous": false,
  "next": "eyJwIjp7...",
  "previous": null,
  "first": "eyJwIjp7...",
  "last": "eyJwIjp7..."
}
```

Follow `next` to walk forward. Reaching page 500 does not read the rows in front of it, and a row
inserted or deleted behind the client cannot make the next page repeat or skip anything. There is
no way to jump to an arbitrary page and no total.

### The four cursors

| cursor | anchor | direction | includes the anchor row |
|---|---|---|---|
| `next` | last row of the page | forward | no |
| `previous` | first row of the page | backward | no |
| `last` | last row of the page | forward | yes |
| `first` | first row of the page | backward | yes |

All four anchor on rows **inside** the page. `first` and `last` are the same two edges read
inclusively: they return their own row again — one duplicate to drop — and cannot skip a row
inserted at that edge. They are present whenever the page is non-empty, including when `next` is
null, so a client polling the head of a live list keeps its anchor.

### What the cursor carries

The full ordering key tuple, with the primary key appended so every tuple is unique. Values travel
as real JSON and are coerced back through the response model's field types.

A cursor is fingerprinted against the ordering and the active filters; sending one to a differently
sorted or filtered query is a 400.

NULLs sit at the end the viewset's `nulls` attribute names — `"first"` (the default) or `"last"` —
**when sorting ascending**. Descending reverses it: `"last"` means NULL is the largest value there
is, so a descending sort puts it in front. That is not SQL's `NULLS LAST`, which stays put; a
backend flips the placement it emits per key.

The in-memory sort, the cursor's comparison and the SQL a backend emits all use this one setting,
which is what stops a cursor walking straight past the NULL rows.

### On a database

The cursor predicate is a filter, so it goes through the same registry and the same all-or-nothing
push-down. The Django backend translates it two ways:

- a **row-value comparison**, `WHERE (year, id) > (2003, 42)`, which a composite index on those
  columns can seek with. Used when the cursor excludes its anchor row, every key sorts the same way,
  and no key is nullable.
- a **union of segments** otherwise, which spells out the NULL handling, copes with mixed
  directions, and adds the anchor row back for the two inclusive cursors.

Row values are supported by Postgres, SQLite 3.15+ and MySQL; not by SQL Server.

## Declarative filters

Declare which fields accept which operators:

```python
from fastapi_viewsets.filters import make_filter_model

TrackFilter = make_filter_model(Track, {
    "year": ["exact", "gte", "lte", "in"],
    "title": ["icontains"],
    "genres": ["overlaps"],
})

class TrackViewSet(CollectionViewSet[int, Track], ListMixin[Track, TrackFilter]):
    list_shape = "cursor"
```

That produces the query parameters `year`, `year__gte`, `year__lte`, `year__in`,
`title__icontains`, `genres__overlaps` — and nothing else. `exact` keeps the bare field name.

An unknown field or operator raises at import time.

Built in: `exact`, `iexact`, `contains`, `icontains`, `startswith`, `gt`, `gte`, `lt`, `lte`, `in`,
`isnull`, `overlaps`.

`in` and `overlaps` take a comma-separated string (`?year__in=1999,2000`); FastAPI cannot expose a
list-typed field of a `Depends()`-expanded model as a query parameter. Values are coerced to the
field's own type, and one that will not convert is a 422.

Hand-written `filter_list` keeps working: a filter model built by hand carries no declaration, and
the pipeline falls through to it.

### Adding an operator

One class with one method, usable on every backend immediately:

```python
from fastapi_viewsets.filters import Filter, register_operator

@register_operator
@dataclass(frozen=True)
class Decade(Filter):
    lookup: ClassVar[str] = "decade"

    def matches(self, record) -> bool:
        value = self.read(record)
        return value is not None and value // 10 * 10 == self.value
```

`matches()` is required and is the universal implementation.

### Teaching a backend to translate one

Compilers are registered against a (backend, filter) pair:

```python
from fastapi_viewsets.filters import compiles
from fastapi_viewsets.backends.django_orm import DjangoORMViewSet

@compiles(DjangoORMViewSet, Decade)
def _(viewset, fltr, queryset):
    return queryset.filter(year__gte=fltr.value, year__lt=fltr.value + 10)
```

A new operator needs no change to any backend, and a new backend needs no change to any filter.

Push-down is all or nothing: if any filter in a request has no compiler, the backend leaves the
whole stage to the in-memory pass.

## Backends

`CollectionViewSet` backs a viewset with anything already in memory. `DjangoORMViewSet` backs one
with a database through the Django ORM:

```python
from fastapi_viewsets.backends.django_orm import DjangoORMViewSet

class TrackViewSet(DjangoORMViewSet[int, Track], CursorListMixin[Track, TrackFilter]):
    model = TrackModel      # Django model
    schema = Track          # pydantic model rows are converted into
```

It returns the queryset unevaluated from `perform_list`, translates the filter set into `.filter()`,
the sort into `.order_by()` in either direction with the NULL placement `nulls` names, and the page
into `LIMIT`/`OFFSET`, marking each stage applied. `count` comes from a `COUNT(*)`. Requires the
`django` extra; every ORM call uses Django's async API or `sync_to_async`.

`pk_field_name` needs no declaration here: the backend takes it from the model's primary key, so a
model keyed on `uuid` or `code` pages correctly without being told. The cursor orders by that name
in SQL and reads its position off the converted record, so the name has to be one both the model and
`schema` carry — `artist_id` rather than `artist` for a one-to-one primary key, and the `id` the
schema carries rather than the parent link for an inherited model. Where `schema` names the key
something else again, assigning `pk_field_name` on the class wins. A composite primary key raises:
no single name expresses it, so name a field that is unique per row on the class instead.

The derivation is a property on `DjangoORMViewSet`, which is why the backend comes first in the base
list, as above. The other order finds `ListMixin`'s plain `"id"` first and the model is never asked.

The `pk_field_name` argument to `route_viewset` is a different thing — it names the field stripped
from the `POST` body — and is passed explicitly.

What it declines and leaves to the in-memory pass: a filter or a sort naming anything but a concrete
model field, and any operator with no registered compiler — `overlaps`, of the built-in ones, whose
Django equivalent is Postgres-only.

A declined stage is handled by the default, never an error.

### Writing your own

Override the stages your store can answer and chain the rest:

| hook | override when |
|---|---|
| `perform_list` | always — return your lazy source |
| `apply_filter` / `apply_sort` | the store can narrow or order |
| `take_page` | the store has LIMIT/OFFSET or a cursor |
| `count_records` | the store can count cheaply |
| `to_record` | your source yields rows rather than the response model |

A stage receives whatever the stage before it passed on — your own lazy object for as long as
nothing has materialised it — and returns records. Four library calls make up the contract:

| call | what it is for |
|---|---|
| `filter_set_for(query)` (`fastapi_viewsets.mixins`) | every filter the request implies: the client's declared ones plus what the pipeline added, the cursor predicate among them. Each filter reports the model fields it reads through `fields()` |
| `filters_from(query.fltr)` (`fastapi_viewsets.filters`) | None when the filter model was not built from a declaration — the viewset is on the hand-written `filter_list` path, and there is nothing to translate |
| `can_compile_all(type(self), filter_set)` / `compile_all(type(self), self, filter_set, queryset)` | whether every filter in the set has a compiler registered for your backend, and applying them in turn to your query object. The last parameter is your backend's query — the signature calls it `query`, and it is not the `ListQuery` the stage was handed |
| `query.mark_applied("filter")` / `query.needs("filter")` | reporting a stage as absorbed, and asking whether it still wants doing |

Chain to `super()` whichever way the decision went. `DjangoORMViewSet.apply_filter`, minus the check
that every filtered name is a concrete model field:

```python
async def apply_filter(self, context, query, records):
    queryset = _as_queryset(records)
    if queryset is None or not query.has_filter or not query.needs("filter"):
        return await super().apply_filter(context, query, records)

    filter_set = filter_set_for(query)
    if not filter_set and filters_from(query.fltr) is None:
        return await super().apply_filter(context, query, records)

    if not can_compile_all(type(self), filter_set):
        return await super().apply_filter(context, query, records)

    queryset = compile_all(type(self), self, filter_set, queryset)
    query.mark_applied("filter")
    return await super().apply_filter(context, query, queryset)
```

`_as_queryset` returning None is how the backend notices that an earlier stage already fell back:
what flows on from there is a list rather than a lazy source, and every push-down below it has to
decline. `apply_sort` and `count_records` open the same way; `take_page` returns `(page, has_more)`
and marks `"pagination"`.

[`land(query, records)`](../api/python-mixins.md) is where the lazy part ends, and an override
decides whether it gets that far: a stage you push down never calls it, the lazy object being the
only thing a filter or an ordering goes into; a filter or a sort you decline is landed by the
default that picks it up. `take_page` and `count_records` never land anything — their defaults walk
the source only as far as the page.

`to_record` runs on the page alone for as long as no stage has landed the source, which means the
filter and the sort each either pushed down or had nothing to do. Pagination is not part of that
condition: decline it and the default `take_page` still cuts its page out of your lazy object, and
the conversion still happens on that page. What declining it costs is the walk to the offset — the
default reads the source row by row until the page is full, and against a database every skipped row
crosses the wire before being discarded. That is cheap at the head of the list and grows with the
offset, which is why `take_page` is worth overriding wherever the store has a real LIMIT/OFFSET.

Conversion sits at the end of the pipeline so the earlier stages carry your query object, and
`to_record` must tolerate an already-converted record: an in-memory stage converts when it
materialises the source, because a filter declaration names the response model's fields while the
source yields whatever the backend stores. That is what makes a declined filter or sort expensive
rather than merely slower — `land()` reads and converts everything that reached it, the whole table
where the filter declined and the filtered set where only the sort did, before the page is cut. The
answer is right either way and the response looks identical, so the only place the difference shows
is `query.applied`, in process: assert on it from your own tests, or a stage you meant to push down
can stop pushing down without anything saying so. An unpaginated request lands the whole source in
any case, there being no page to cut.
