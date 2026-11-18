# adorm Backend

`AdormViewSet` (`fastapi_viewsets.backends.adorm`) backs a viewset with
[adorm](:adorm:/), the asynchronous ORM interface. Every operation is an awaited adorm
query on an adorm session: the request's coroutine waits on adorm's pool, without a thread per request and without
`sync_to_async`. The records are adorm models, which are pydantic models, so a model `adorm generate` writes from a
Django model is the viewset's schema as it is.

Filters, the sort and the page become SQL of adorm queries; writes run in transactions of the session. adorm's
`DoesNotExist` and `IntegrityError` of the viewset's operations, and of any code of its actions under
[`AdormSession`](#adormsession-the-session-of-the-whole-action), answer as `ApiErrorBody`; adorm's `ValidationError`
answers as `ApiErrorBody` wherever it is raised.

## Installation

adorm with a driver, on Python 3.11+. The extra `adorm` installs adorm with both drivers: `asyncpg` (native, async)
and `django` (Django's ORM, on a thread per connection).

```bash
pip install "dynamicforms-fastapi-viewsets[adorm]"
pip install dynamicforms-fastapi-viewsets "adorm[django]"     # the django driver only
```

Both drivers describe the models with Django, so the process sets Django up and configures adorm's connection
profiles before the first request:

```python
# myproject/main.py
import myproject.django_setup  # noqa: F401,I001 - django.setup(), see Django Integration

from contextlib import asynccontextmanager

import adorm

from adorm.settings import close_pools
from fastapi import FastAPI

adorm.configure(profiles={"default": {"driver": "adorm.drivers.asyncpg.AsyncpgDriver", "alias": "default"}})


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_pools()


app = FastAPI(lifespan=lifespan)
```

## Declaring a viewset

`model` is the adorm model. Without `schema`, the model is also the schema of the records:

```python
# myproject/adorm_models/clubs.py, written by `adorm generate`
class Member(Model):
    _meta = ModelMeta("clubs.Member", schema_hash="a03a24101f1e7c84", orm_names={"copy_": "copy"})
    id: int | None = None
    name: str = Field(max_length=100)
    email: str = Field(max_length=100)
    rating: int | None = None
    club_id: int | None = None
    copy_: str = Field(default="", alias="copy", max_length=20)
    club = ForeignKey("clubs.Club")
```

```python
from fastapi_viewsets.backends.adorm import AdormViewSet
from fastapi_viewsets.decorators import route_viewset
from fastapi_viewsets.filters import make_filter_model
from fastapi_viewsets.mixins import BulkViewSetMixin, CursorListMixin, LookupMixin

from myproject.adorm_models.clubs import Member

MemberFilter = make_filter_model(Member, {"name": ["icontains"], "rating": ["gte", "isnull"], "club_id": ["in"]})


@route_viewset(router, base_path="/members", pk_field_name="id")
class MemberViewSet(
    AdormViewSet[int, Member],
    BulkViewSetMixin[int, Member, MemberFilter],
    CursorListMixin[Member, MemberFilter],
    LookupMixin,
):
    model = Member
    lookup_title = "name"
```

The backend precedes the mixins in the bases, as `DjangoORMViewSet` does: `pk_field_name` is a property of the
backend, the model's primary key field when the schema has it.

A generated model's fields are its columns: a foreign key is the field `club_id`, and the relation `club` is not part
of the schema. A field renamed for pydantic (`copy_`) keeps its column's name as its alias, so the JSON and the
OpenAPI schema carry `copy`:

```json
{"id": 4, "name": "Dana", "email": "dana@example.com", "rating": null, "club_id": null, "copy": ""}
```

Clients use that name everywhere: the list sorts and filters by `copy` (`?sort=copy:desc`) in SQL, and a field
error of the column goes under `copy`.

### Another schema

A viewset can answer another pydantic model, from its own query set. `to_record` converts a row: a model instance, a
dict of a `values()` query set, or an instance of the schema, which it returns as it is. The default validates the
schema from the row's attributes or keys:

```python
from adorm import Concat, F, Value


class MemberRow(BaseModel):
    id: int
    list_name: str
    club_name: str | None
    rated: bool


class MemberListViewSet(AdormViewSet[int, MemberRow], PaginatedListMixin[MemberRow]):
    model = Member
    schema = MemberRow

    async def perform_list(self, context):
        return (
            (await self.get_queryset(context))
            .annotate(list_name=Concat(F("name"), Value(" <"), F("email"), Value(">")), club_name=F("club__name"))
            .order_by("list_name", "id")
            .values("id", "list_name", "club_name", "rating")
        )

    async def apply_sort(self, context, query, records):
        query.mark_applied("sort")          # the pages keep the query set's own order
        return await super().apply_sort(context, query, records)

    def to_record(self, raw):
        if isinstance(raw, MemberRow):
            return raw
        return MemberRow(id=raw["id"], list_name=raw["list_name"], club_name=raw["club_name"],
                         rated=raw["rating"] is not None)
```

The page of such a list is one query with `LIMIT`/`OFFSET`, and `count` one `COUNT(*)` of the same query set.
`get_queryset` stays the plain query set of model instances: retrieve, update and destroy read through it.

## The session of a call

Every operation of a call runs on the session `get_session(context)` returns. It is the current session of the
context (`adorm.current_session()`, the session `("default", "default")` unless the application activated another),
or the session of `using` and `session_name`:

```python
class ReportViewSet(AdormViewSet[int, Member], PaginatedListMixin[Member]):
    model = Member
    using = "reports"              # adorm.create_session("reports", "default")
```

Both are coroutines, so an override awaits the context's values. Override `get_session` to choose the session per
call, for example a connection profile per user that a profile provider creates:

```python
class MemberViewSet(AdormViewSet[int, Member], BulkViewSetMixin[int, Member]):
    model = Member

    async def get_session(self, context):
        user = await context.user
        return adorm.create_session(f"user:{user.pk}", "api")
```

`get_queryset(context)` binds the model to that session and runs no query; override it to scope every operation. A
row it excludes is not found by retrieve, update and destroy:

```python
    async def get_queryset(self, context):
        queryset = await super().get_queryset(context)
        return queryset.filter(club_id=await context.club_id)
```

### `AdormSession`: the session of the whole action

The command middleware `AdormSession` makes the viewset's session current while the action runs, so adorm code in an
overridden method, a custom endpoint and the tasks they start (`asyncio.gather`, `TaskGroup`) uses it through
`Model.objects`. With `atomic=True` the action runs in one `atomic()` block of the session: an exception rolls back
every write of the action, also those of its tasks, and the writes of `perform_*` become savepoints of it.

Under `AdormSession`, adorm's `ObjectDoesNotExist` raised anywhere in the action answers 404
`fastapi-viewsets.not_found` (`{pk}` the action's `pk`, null without one), and `IntegrityError` a 422 field error as
in [Errors](#errors). A foreign key PostgreSQL checks at the commit of the action's block goes under the field of the
row the viewset wrote with the value the database names (`1.club_id` in a bulk create), as without the middleware.
Without `AdormSession`, these exceptions of an action's own code answer 500 `fastapi-viewsets.unhandled_error`.

```python
from fastapi_viewsets.backends.adorm import AdormSession
from fastapi_viewsets.conf import settings
from fastapi_viewsets.events import ChangeEvents

# ChangeEvents first: the transaction commits before it publishes, and a rollback publishes nothing.
settings.viewsets_command_middleware = [ChangeEvents(), AdormSession(atomic=True)]
```

```python
class SignupViewSet(AdormViewSet[int, Member], CreateMixin[int, Member]):
    __router = APIRouter()
    model = Member

    @__router.post("pair")
    async def pair(self, first: str, second: str) -> list[int]:
        a, b = await asyncio.gather(
            Member.objects.create(name=first, email=f"{first}@example.com"),
            Member.objects.create(name=second, email=f"{second}@example.com"),
        )
        return [a.pk, b.pk]         # both rows or neither
```

The value of `@action_configuration({AdormSession: ...})` replaces `atomic` for a viewset or an action:

```python
@action_configuration({AdormSession: False})
class ImportViewSet(AdormViewSet[int, Member], BulkViewSetMixin[int, Member]):
    model = Member
```

It runs around the action in the process that executes it, a `celery_viewset` worker included, and leaves the actions
of other viewsets as they are.

## List pipeline

`perform_list` returns `get_queryset(context)` unevaluated, and each stage adds to its SQL, marking itself applied:

| Stage | SQL |
|---|---|
| filter | `filter(field__lookup=value)` for each declared filter: `exact`, `iexact`, `contains`, `icontains`, `startswith`, `gt`, `gte`, `lt`, `lte`, `in`, `isnull` |
| sort | `order_by(F(field).asc(nulls_first=True), ...)`, NULL placed as `nulls` names (descending reverses it) |
| page | the slice `[offset:offset + limit + 1]`, one row past the page answers `has_more` |
| count | `count()` |
| cursor | the cursor predicate as a union of per-key conditions, NULL placement and mixed directions spelled out |

A filter set is taken into SQL only as a whole: every filter must name a field of `model` and have a compiler.
Otherwise the in-memory pass filters, as it sorts when a sort column is not a field of `model`; `overlaps` has no
adorm lookup and always runs in memory. A filter of your own compiles with `compiles(AdormViewSet, ...)`, as for any
backend (see [Writing your own](./list-pipeline#writing-your-own)):

```python
from fastapi_viewsets.filters import compiles


@compiles(AdormViewSet, Decade)
def _decade(viewset, fltr, queryset):
    return queryset.filter(year__gte=fltr.value, year__lt=fltr.value + 10)
```

## Lookup

With `LookupMixin`, `perform_lookup` returns `get_queryset(context)`. With `lookup_title` (and `lookup_group`) set,
it is ordered by group, title and primary key, each row becomes a `LookupItem`, and the default `LookupFilter`
filters in SQL: `q` as `<lookup_title>__icontains`, `ids` as `pk__in` (a value that is not a primary key of the
model's type is left out).

```python
class ClubViewSet(AdormViewSet[int, Club], LookupMixin):
    model = Club
    lookup_title = "name"
    lookup_group = "region"
```

```text
GET /clubs/lookup?q=alp          → [{"group": "North", "pk": 1, "title": "Alpine", "icon": null}]
GET /clubs/lookup?ids=1&ids=7    → the clubs 1 and 7
```

Without `lookup_title`, `to_lookup_item` is yours to override and the filter runs in memory.

## Writes

| Action | What runs |
|---|---|
| create, bulk create | in one transaction, each record validated as `model` and saved in a savepoint; the rows are read back, so the answer carries what the database filled in |
| update, partial update, bulk update | in one transaction: each key checked against `get_queryset` (404 before any lock), each row locked with `SELECT ... FOR UPDATE` in key order, the record's fields set (for `PATCH` the stated ones), the result validated as `schema`; then each row saved in a savepoint (all its fields, so `auto_now` fields and a `save()` override apply) and read back, so the answer is the row as stored |
| destroy | the model's delete of the row (its cascades included); bulk destroy deletes each key on its own |

A create writes the primary key only when the record carries one; a record without it gets the database's value. An
update never writes the primary key. One record that fails writes none of a bulk action's records. Two concurrent
updates of the same row take turns: the second reads the first one's result, so neither reverts the other's fields.
Overlapping bulk updates lock their rows in the same order and do not deadlock.

`clean_on_save = True` runs `full_clean()` before each save: on the target, Django's `Model.full_clean()` with its
field validators, `clean()`, unique checks and constraints, in the transaction. On the Django driver, Django runs
those checks' queries on the database its routers name for the model; a profile whose alias is not `default` needs a
router that sends the model there.

```python
class MemberViewSet(AdormViewSet[int, Member], BulkViewSetMixin[int, Member]):
    model = Member
    clean_on_save = True
```

## Errors

| Raised | Answer |
|---|---|
| `DoesNotExist` of retrieve, update or destroy, or a row `get_queryset` excludes | 404 `fastapi-viewsets.not_found` (in bulk destroy, the key's entry) |
| a record that fails `model` or `schema` | 422 `fastapi-viewsets.validation_failed`, pydantic's errors |
| `IntegrityError` of a save, its constraint read from PostgreSQL's report | 422, see below |
| `ValidationError` of `full_clean()` | 422, `fastapi-viewsets.invalid` with adorm's message in `detail` (adorm does not carry Django's code and params) |
| `ValidationError` raised anywhere in a request | 422, as Django's `ValidationError` (see [Error Codes](./error-codes)) |

A violated constraint is a field error under the field of the column the database names, by the name clients know
it by (`copy` for the generated `copy_`):

| Constraint | Field error |
|---|---|
| unique (`23505`) | `fastapi-viewsets.unique` `{fields}`, under the field when the key is one column, else on the form |
| `NOT NULL` (`23502`) | `fastapi-viewsets.required`, under the field |
| foreign key (`23503`) | `fastapi-viewsets.invalid` ("The related record does not exist."), under the field |
| `PROTECT` / `RESTRICT` reference of a destroy | `fastapi-viewsets.protected` / `fastapi-viewsets.restricted`, on the form |
| any other (a check constraint) | `fastapi-viewsets.invalid`, on the form |

```json
{
  "detail": "The request failed validation",
  "detail_code": "fastapi-viewsets.validation_failed",
  "detail_params": {},
  "field_errors": {"email": [{"code": "fastapi-viewsets.unique", "params": {"fields": ["email"]},
                              "detail": "A record with this value already exists."}]}
}
```

In a bulk action the key carries the row: the index for a create (`1.email`), the primary key for an update
(`7.email`). `full_clean()` reports a foreign key under its relation (`club`); with the column in the schema, the
error goes under the column (`club_id`). PostgreSQL checks the foreign keys Django creates at the commit; such an
error goes under the rows whose value the database names.

## Cancellation and failures

A request whose task is cancelled (the client went away, a timeout of the application) cancels its adorm operation:
a statement waiting for a lock stops in the database, the transaction rolls back and the slot returns to the pool.
adorm's connection options apply to the operations of a call:

```python
with adorm.connection_options(lock_timeout=2):
    await viewset.perform_update(context, pk, data)     # adorm.OperationalError after 2 s of waiting
```

An error adorm has no answer for (a timeout, a lost connection) is a 500 `fastapi-viewsets.unhandled_error`; adorm
replaces a broken connection for the next request.

## Reference

| Attribute or method | Meaning |
|---|---|
| `model` | the adorm model the viewset reads and writes |
| `schema` | the pydantic model of the records; `model` when not set |
| `using`, `session_name` | the session of the viewset's operations; `using = None` (default) is the current session |
| `clean_on_save` | whether creates and updates run `full_clean()` before saving; default False |
| `lookup_title`, `lookup_group` | the fields of a lookup choice's title and group |
| `async get_session(context)` | the session of a call's operations |
| `async get_queryset(context)` | the base query set of model instances on that session |
| `to_record(raw)` | a row as `schema` |
| `AdormSession(atomic=False)` | the command middleware that makes the viewset's session current for each action, in one `atomic()` block with `atomic` |

## Demo

With a PostgreSQL server named, the demo serves the music library from PostgreSQL through `AdormViewSet` at
`/music-adorm` and through `DjangoORMViewSet` at `/music-db`, both on the same table, next to the in-memory one, on
Python 3.11+ (where the dev dependencies include adorm):

```bash
uv run python demo.py --db postgres://user:password@localhost:5432/
uv run python demo.py --db postgres://user:password@localhost:5432/ --adorm-driver django
uv run python demo.py --db postgres://user:password@localhost:5432/ --benchmark
```

It creates and fills the database `adorm_fastapi_viewsets_demo` on that server. The records are the generated adorm
model of `demo/backend/adorm_tables.py`, whose array columns are the records' lists. Without a server `/music-db`
serves SQLite and the grid's **PostgreSQL (adorm)** button is disabled; without adorm only that button is disabled.
`GET /backends` says why. A server that cannot be reached at startup leaves `/music-db` on SQLite and the adorm button
disabled, as without a server. A server that is reachable but whose database cannot be set up turns both PostgreSQL
data sources off, and they answer 503.

The demo's **Benchmark** tab compares the two DB APIs on that table under load. The benchmark's viewsets read through
the Django alias and the adorm profile `bench`, which connect through a proxy that adds a round-trip time to every
query (`--db-latency`, 10 ms by default). The adorm viewset runs with `@action_configuration({AdormSession: False})`,
so a read is one query outside a transaction, as the Django ORM's autocommit read is; its pool holds
`DEMO_ADORM_POOL` connections (40) per process. The Django viewset's alias keeps its connections (`CONN_MAX_AGE =
None`). The FastAPI executor serves the viewsets from uvicorn processes of their own (`--bench-processes`, 2 by
default). `--benchmark` adds the Celery executors: a classic worker and one with async processes (Redis needed).
