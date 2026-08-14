# The list pipeline

`GET /{base_path}` runs the records through four steps, each of which a viewset can replace:

```
perform_list  →  apply_filter  →  apply_sort  →  apply_pagination
```

`get_list` is the whole pipeline in one method, so a viewset that wants to rearrange it can
override that instead of the individual stages.

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

Returning a plain list still works and always will; it is what `CollectionViewSet` does, because an
in-memory collection has nothing to gain from laziness.

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

`query.mark_applied("sort")` is how a backend reports what it already did, so the in-memory default
leaves the rows alone instead of re-sorting them. That is what makes push-down composable: a
viewset can push down the sort, leave the filter to the default, and neither stage needs to know
about the other.

Stage names are `"filter"`, `"sort"` and `"pagination"`.

### ListQuery

| field | meaning |
|---|---|
| `fltr` | the filter model, or None when the client sent no filter |
| `sort` | list of `SortStateColumn`, in priority order |
| `offset` / `limit` | the requested page; `limit` is None when not paginated |
| `applied` | stages a backend has already handled |

A filter model whose every field is None counts as *no filter*: FastAPI builds the model whether or
not the client sent anything in it, so a null check cannot answer the question.

## Replacing setup_filter and setup_sort

The older `setup_filter` / `setup_sort` hooks ran before `perform_list` and worked by mutating
instance state, because `perform_list` had no way to be told what was being asked of it. That meant
every push-down was a pair of methods — one to stash the query, one to apply the leftovers — which
had to be kept in step by hand.

They still work and now emit a `DeprecationWarning`. The replacement is a single `apply_*`
override, which receives the query and the records together:

```python
# before
async def setup_sort(self, sort: SortState) -> None:
    self._pending_sort = sort

async def sort_list(self, sort, records):
    return records if self._pending_sort_was_applied else in_memory_sort(records)

# after
async def apply_sort(self, context, query, records):
    if query.has_sort:
        records = self.db.order_by(...)
        query.mark_applied("sort")
    return await super().apply_sort(context, query, records)
```

`filter_list` and `sort_list` are not deprecated: they remain the place to implement in-memory
filtering and sorting, and the default `apply_*` stages call them.

## Declaring a shape

A list endpoint answers in one of three shapes, and they are different contracts rather than
variations on a theme:

| shape | response | good for | bad at |
|---|---|---|---|
| `plain` | a bare array | a lookup table | anything that grows |
| `paginated` | `{results, offset, limit, count, …}` | jumping to page 40 | re-reads the 39 in front; drifts on insert |
| `cursor` | `{results, next, previous, first, last, …}` | walking a live list | cannot jump, cannot count |

The viewset says which:

```python
class TrackViewSet(CollectionViewSet[int, Track], ListMixin[Track, TrackFilter]):
    list_shape = "cursor"
```

`PaginatedListMixin` and `CursorListMixin` are shorthands for exactly that. Unset, the shape comes
from `settings.default_list_shape`, which starts at `"plain"`.

Everything else follows from the declaration. An endpoint that answers `cursor` advertises `cursor`
and `limit` and no `offset`; one that answers `plain` advertises neither; and the response is one
model, not a union:

```
plain      →  {"$ref": ".../ListOf_Track_"}
cursor     →  {"$ref": ".../CursorPage_Track_"}
```

### Letting the client choose

Add `list_shapes` and a client may ask for any of them with an `X-List-Shape` header:

```python
class TrackViewSet(CollectionViewSet[int, Track], ListMixin[Track, TrackFilter]):
    list_shape = "cursor"                            # the default
    list_shapes = ("cursor", "plain", "paginated")   # what a client may request
```

The endpoint then takes the header, the response becomes a union of exactly those models, and each
one is documented with an example named after the header value that produces it — so the docs say
`plain` rather than `ListOf_Track_`. A value the viewset did not list is a 422.

This is worth doing when clients genuinely differ — a grid wants cursor paging, an export wants the
lot. It is not free: the schema becomes a union every generated client has to narrow, and OpenAPI
cannot express that the choice depends on a request header, so that part stays prose. Declaring one
shape is the normal case.

## Pagination

Use `PaginatedListMixin` in place of `ListMixin`:

```python
class TrackViewSet(CollectionViewSet[int, Track], PaginatedListMixin[Track, TrackFilter]):
    default_page_size = 50
    max_page_size = 500
```

The endpoint gains `offset` and `limit` parameters and answers with an envelope:

```json
{
  "results": [...],
  "offset": 50,
  "limit": 50,
  "count": 5000,
  "has_more": true,
  "has_previous": true
}
```

Paging is a viewset-wide decision, not a per-request one. An endpoint that answers sometimes with a
list and sometimes with an envelope forces every client to branch on the shape it got back, and
leaves the OpenAPI schema describing a union that nothing can be generated from — so the two live
on separate mixins with one clean shape each.

`count` is null when nobody knows it. A generator cannot be counted without draining it, and
draining it is precisely what paging exists to avoid. `has_more` and `has_previous` are stated
outright rather than left to be inferred from a null link, because inference goes wrong exactly at
the boundary that matters.

Paging a lazy source stays lazy: it reads `limit + 1` rows — the extra one is what answers
`has_more` — so a page out of a million-row generator costs `offset + limit + 1` steps, not a
million.

`limit` is capped at `max_page_size`, so a client cannot turn paging back into "fetch everything"
by asking for a page the size of the collection.

### On the client

```ts
const page = await tracks.listPage({ offset: 0, limit: 50, sort: 'year:desc' });
page.results;      // Track[]
page.hasMore;      // the envelope's snake_case fields are renamed; records are untouched
```

## Declarative filters

Instead of writing `filter_list` by hand, declare which fields accept which operators:

```python
from fastapi_viewsets.filters import make_filter_model

TrackFilter = make_filter_model(Track, {
    "year": ["exact", "gte", "lte", "in"],
    "title": ["icontains"],
    "genres": ["overlaps"],
})

class TrackViewSet(CollectionViewSet[int, Track], PaginatedListMixin[Track, TrackFilter]):
    ...
```

That produces the query parameters `year`, `year__gte`, `year__lte`, `year__in`,
`title__icontains`, `genres__overlaps` — and nothing else. `exact` keeps the bare field name,
because `?year=2003` is what anyone would write. Generating every field crossed with every operator
would put dozens of meaningless parameters in the schema, so the declaration is explicit.

A typo is refused at import time rather than producing a parameter that silently never matches.

Built in: `exact`, `iexact`, `contains`, `icontains`, `startswith`, `gt`, `gte`, `lt`, `lte`, `in`,
`isnull`, `overlaps`.

`in` and `overlaps` take a **comma-separated string** (`?year__in=1999,2000`), not repeated
parameters. FastAPI cannot expose a list-typed field of a `Depends()`-expanded model as a query
parameter — it drops the field from the schema and never populates it, with or without an explicit
`Query()` — and that expansion is how `route_viewset` turns a filter model into individual query
parameters. Values are still coerced to the field's own type; one that will not convert is refused
rather than dropped.

### Adding an operator

One class with one method, and it works on every backend immediately:

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

`matches()` is mandatory and is the universal implementation. Everything else is optional.

### Teaching a backend to translate one

Backend translations are registered, not inherited — they live with the backend, not with the
filter:

```python
from fastapi_viewsets.filters import compiles
from fastapi_viewsets.backends.django_orm import DjangoORMViewSet

@compiles(DjangoORMViewSet, Decade)
def _(viewset, fltr, queryset):
    return queryset.filter(year__gte=fltr.value, year__lt=fltr.value + 10)
```

That asymmetry is the whole design. If a filter carried an implementation per backend, adding a
backend would mean editing every filter; if a backend carried one per filter, adding an operator
would mean editing every backend. Registered pairs close neither door.

Push-down is **all or nothing**: if any filter in a request has no compiler, the backend leaves the
entire stage to the in-memory pass. Translating the ones it understands and forgetting the rest
would return too many rows while looking like it worked.

Hand-written `filter_list` keeps working untouched — a model built by hand carries no declaration,
so this machinery stays out of its way. It remains the right answer for anything an operator would
express badly.

## Backends

`CollectionViewSet` backs a viewset with anything already in memory. `DjangoORMViewSet` backs one
with a database through the Django ORM, and is what the push-down machinery above was designed
against:

```python
from fastapi_viewsets.backends.django_orm import DjangoORMViewSet

class TrackViewSet(DjangoORMViewSet[int, Track], PaginatedListMixin[Track, TrackFilter]):
    model = TrackModel      # Django model
    schema = Track          # pydantic model rows are converted into
```

It returns the queryset unevaluated from `perform_list`, translates exact filters into `.filter()`,
ascending sorts into `.order_by()`, and the page into `LIMIT`/`OFFSET`, marking each stage applied
so the in-memory defaults do not repeat the work. `count` comes from a real `COUNT(*)` instead of
being reported as unknown. Requires the `django` extra; nothing blocks the event loop, since every
ORM call uses Django's async API or `sync_to_async`.

Two things it deliberately declines:

- **Descending sorts.** This library defines NULL as the smallest value in *both* directions; SQL's
  `DESC` puts NULLs first. Django can express the fix, but only per database backend, so the honest
  answer is to leave it to the in-memory sort, which already implements the intended semantics.
- **Anything but exact matches.** Operators belong in the filter plugin API rather than in a
  private lookup syntax that would have to be unpicked later.

Both fall back rather than failing, which is the contract: an unapplied stage is a stage the
default handles, never an error.

### Writing your own

Override the stages your store can answer and chain the rest:

| hook | override when |
|---|---|
| `perform_list` | always — return your lazy source |
| `apply_filter` / `apply_sort` | the store can narrow or order |
| `take_page` | the store has LIMIT/OFFSET or a cursor |
| `count_records` | the store can count cheaply |
| `to_record` | your source yields rows rather than the response model |

`to_record` must tolerate being handed an already-converted record: an in-memory stage converts
when it materialises the source, because a filter declaration names the *response* model's fields
while the source yields whatever the backend stores. Where the two shapes differ — a list kept as a
comma-joined column, say — filtering the raw rows would read the wrong thing entirely.

`to_record` runs on the page only, never on the whole source. That is why conversion happens at the
end: the earlier stages must carry your query object, because that is the only thing a filter or an
ordering can be pushed into.

## Cursor pagination

`CursorListMixin` pages by *where you are* rather than by *how many you skipped*:

```python
class TrackViewSet(CollectionViewSet[int, Track], CursorListMixin[Track, TrackFilter]):
    schema = Track          # the response model, used to coerce cursor values back from JSON
    pk_field_name = "id"    # what makes every key tuple unique
    default_page_size = 50
```

The endpoint takes `cursor` and `limit` — no `offset` — and answers:

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

Follow `next` to walk forward. What that buys over offset paging is two things offset paging
cannot do: reaching page 500 does not re-read the 24 950 rows in front of it, and a row inserted
or deleted behind you cannot make the next page repeat or skip anything.

What it costs is jumping to an arbitrary page, and a total count — producing one is a second full
pass on every request, and the number is stale by the time it is read.

### Anchors are rows in the page, not the rows beside it

DRF encodes the position of the record *before* the page into `previous`. Anything inserted between
that record and the page's own first row then sits in a gap: the boundary moved, and paging back
steps straight over it.

`first` and `last` anchor on the page's **own** first and last rows, which cannot move like that.
They are the same two edges as `next`/`previous` but read *inclusively*, so they return their own
row again — one duplicate for the client to drop — and in exchange they cannot skip anything that
arrived at that edge. They are also present whenever the page is non-empty, so a client polling the
head of a live list keeps its anchor even when `next` is null.

| cursor | anchor | direction | includes the anchor row |
|---|---|---|---|
| `next` | last row | forward | no |
| `previous` | first row | backward | no |
| `last` | last row | forward | yes |
| `first` | first row | backward | yes |

### What the cursor carries

The **whole ordering key tuple**, not just the first key — which is what makes multi-key ordering
work and what removes ties instead of counting through them. The primary key joins the ordering
automatically, so every tuple is unique and every position holds exactly one row.

Values travel as real JSON (`null` stays `null` rather than needing a sentinel string, which is a
value a record could legitimately hold) and are coerced back through the response model's own field
types on the way in.

A cursor is fingerprinted against the ordering and the active filters. Send one to a differently
sorted or filtered query and it is refused with a 400 rather than quietly describing a page nobody
asked for.

NULL is defined as the smallest value, in both directions. That is not SQL's rule — SQL makes every
comparison with NULL unknown — but a definite answer is the only way a nullable column can take
part in a total order at all.

### On a database

The predicate is a filter like any other, so it goes through the same registry and the same
all-or-nothing push-down. The Django backend translates it two ways:

- a **row-value comparison**, `WHERE (year, id) > (2003, 42)`, which a composite index on exactly
  those columns can seek with. Used only when every key sorts the same way — a row constructor
  cannot express `a ASC, b DESC` — and no key is nullable, since SQL's NULL rule and this
  library's disagree and SQL's wins inside the database.
- a **union of segments** otherwise, which spells the NULL handling out and copes with mixed
  directions, at the cost of a plan the optimiser usually degrades to a scan.

Support for row values: Postgres, SQLite 3.15+, MySQL. Not SQL Server.

### On the client

```ts
let page = await tracks.listCursor({ sort: 'year:asc', limit: 50 });
while (page.next) page = await tracks.listCursor({ sort: 'year:asc', cursor: page.next });
```
