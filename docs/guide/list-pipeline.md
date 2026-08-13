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

`to_record` runs on the page only, never on the whole source. That is why conversion happens at the
end: the earlier stages must carry your query object, because that is the only thing a filter or an
ordering can be pushed into.

## Cursor pagination

Not yet implemented — see `TODO.md`. Offset paging re-reads skipped rows and drifts when records
are inserted or deleted between pages; a cursor fixes both by carrying an anchor instead of a
count. It needs the comparison predicate to reach whatever produces the rows, which for a
Celery-backed or database-backed viewset means reaching the worker, and that part is still being
designed.
