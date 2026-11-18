# DynamicForms FastAPI Viewsets

Django REST Framework-style viewsets for [FastAPI](https://fastapi.tiangolo.com/), with optional
Celery-backed async execution and a matching Vue/TypeScript client counterpart.

- **Python mixins for FastAPI** — compose CRUD and bulk endpoints from small, focused mixin classes.
- **`route_viewset` decorator** — register a viewset on a FastAPI router with a single decorator call.
  Handles type resolution, lifecycle management and OpenAPI schema automatically.
- **Nested viewsets** — mount a viewset under another one's path (`/accounts/members`) with
  `ViewSetRouter.sub_viewset`; a request to it passes the parent's checks and its own.
- **`CollectionViewSet`** — zero-boilerplate in-memory viewset backed by any Python list, set or dict.
  Great for prototyping and testing.
- **`DjangoORMViewSet`** — back a viewset with a Django QuerySet. The filters it can compile, the
  sort order with its NULL placement, the page as LIMIT/OFFSET and the total as `COUNT(*)` all
  become SQL; a stage it declines falls back to the in-memory pass instead of failing, and
  `pk_field_name` comes from the model's primary key (requires the `django` extra).
- **`AdormViewSet`** — back a viewset with [adorm](https://docs.velis.si/adorm/), the asynchronous ORM
  interface: filter, sort, page and count become SQL of adorm queries awaited on adorm's pool, writes run in
  transactions with row locks, database constraints answer field errors, and a model `adorm generate` writes is the
  viewset's schema (requires adorm with a driver).
- **`celery_viewset` decorator** — move a viewset's execution to a Celery worker with no code changes
  to the viewset itself, for long-running or background processing scenarios (requires the `celery`
  extra).
- **Bulk operations** — first-class support for bulk create, update, partial update and destroy.
- **muxws transport** — reach the same viewsets over a single WebSocket instead of one HTTP request
  per call. A command goes through your application as an HTTP request does - its middleware,
  exception handlers, router dependencies and state - so both transports behave identically.
- **Events** — publish an event to a topic and every frontend following it learns of it, as a muxws
  push or on its next poll. `ChangeEvents` publishes every create, update and destroy of a viewset to
  its routed base path; custom events are declared with `source()`. Every event is recorded before it
  is pushed, so a dropped push costs latency, not an event. On the frontend, `watch()` on a ViewSet
  follows its topic and `applyChanges()` applies each change to a list of rows.
- **Three list shapes** — a bare array, offset paging, or cursor paging. A viewset declares which,
  and may let a client pick per request with an `X-List-Shape` header.
- **Declarative filters** — declare which fields accept which operators and get query parameters,
  an OpenAPI schema and filtering for free; backends translate what they can into their own query.
- **Vue / TypeScript counterpart** — mirror mixin classes and the `restViewSet` / `muxwsViewSet` class
  factory give you a fully typed client that matches your backend viewset exactly: the mixins a ViewSet
  declares are its public surface, so calling an action it did not declare is a compile error rather
  than a runtime 404 (published separately as
  [`@dynamicforms/fastapi-viewsets`](https://www.npmjs.com/package/@dynamicforms/fastapi-viewsets) on npm).

## Installation

```bash
pip install dynamicforms-fastapi-viewsets

# with Celery-backed viewset support
pip install "dynamicforms-fastapi-viewsets[celery]"

# with the muxws WebSocket transport
pip install "dynamicforms-fastapi-viewsets[muxws]"

# with the Django ORM backend (Django 4.2+ and asgiref)
pip install "dynamicforms-fastapi-viewsets[django]"

# with ICU collations for the in-memory sort (PyICU; builds from source: needs libicu-dev, pkg-config and g++)
pip install "dynamicforms-fastapi-viewsets[icu]"
```

Requires Python 3.10+, FastAPI and Pydantic 2.11+.

## Quick example

```python
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.decorators.route_viewset import route_viewset
from fastapi_viewsets.mixins import BulkViewSetMixin


class Item(BaseModel):
    id: int
    name: str


database: dict[int, Item] = {1: Item(id=1, name="First element")}

app = FastAPI()
router = APIRouter()


@route_viewset(router, base_path="/items", pk_field_name="id")
class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item]):
    def __init__(self):
        super().__init__(container=database, pk_field="id")


app.include_router(router)
```

See the [full documentation](https://docs.velis.si/dynamicforms/fastapi-viewsets/) for guides on
the mixin system, `route_viewset`, `CollectionViewSet`, `DjangoORMViewSet`, `AdormViewSet`, `celery_viewset`, the
list pipeline and pagination, the muxws transport, events, and the Vue client.

## Demo

An infinite-scrolling grid over a 5000-track library, cursor-paged. Switch the whole grid between
the REST and muxws transports and between the in-memory, Django ORM (SQLite, or PostgreSQL with `--db`) and adorm
(PostgreSQL) data sources, and compare their latency side by side. Sorting and filtering are server-side.

The grid follows its viewset's change events (`ChangeEvents` on the backend, `watch()` in the
grid). Click a Favorite cell to toggle it: every tab showing the library applies the change, over
muxws as a push, over REST on its next poll. A sorted or filtered grid reloads its list on the
change: the row may have moved or left it.

With `--celery` the worker runs the viewset calls of the in-memory and Django ORM data sources and
publishes their events through Redis. It runs as one process, because the in-memory library is a dict
held by each process; the Django ORM library (SQLite or PostgreSQL) is shared by every process. The
end-to-end suite starts its own backend and dev server on their own ports.

The **Benchmark** tab measures the same read-only viewset on the Django ORM (`DjangoORMViewSet`) and on adorm
(`AdormViewSet`), over one PostgreSQL table, run by FastAPI in-process (one uvicorn process), by a classic Celery
worker (2 prefork processes) and by a Celery worker with async processes (2 prefork + 2 async processes,
`--aconcurrency 200`), over REST and muxws, at 10, 100 and 1000 concurrent clients. A proxy in front of PostgreSQL
adds a round-trip time to every benchmark query (`--db-latency`, 10 ms by default, adjustable in the tab), standing
in for a remote database. The load generator runs in a process of its own on the same machine. The FastAPI executor
needs `--db`; the Celery executors need `--benchmark` and Redis.

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/dynamicforms/fastapi-viewsets?quickstart=1)

### Run locally

Requirements: Python 3.10+, [uv](https://docs.astral.sh/uv/), Node 22+; Redis for `--celery` and `--benchmark`
(`redis://localhost:6379/0`; a different URL goes in the `DEMO_REDIS_URL` environment variable); Python 3.11+ and
a PostgreSQL server for `--db` (`max_connections` of at least 300 for the benchmark).

```bash
git clone https://github.com/dynamicforms/fastapi-viewsets.git && cd fastapi-viewsets
uv sync && npm ci
uv run python demo.py                # backend on :8000, frontend on :5173
uv run python demo.py --celery       # ... with every viewset call routed through a Celery worker
# DEMO_REDIS_URL=redis://redis-host:6379/0 uv run python demo.py --celery   # Redis elsewhere
uv run python demo.py --db postgres://user:password@localhost:5432/
                                     # ... and the library on PostgreSQL, through the Django ORM and adorm
uv run python demo.py --db postgres://user:password@localhost:5432/ --benchmark
                                     # ... and the benchmark's Celery workers
npm run test:e2e                     # drives the demo in a browser
```

`DEMO_DATABASE_URL` can stand in for `--db`; `DEMO_API_PORT` and `DEMO_FE_PORT` move the backend and the frontend
to other ports; `DEMO_ADORM_POOL` sets the adorm pool size of each benchmark process (40). With `DEMO_DATABASE_URL`
set, `npm run test:e2e` starts the backend through `demo.py` with the latency proxy, and with `DEMO_REDIS_URL` set as
well, with the benchmark's Celery workers; otherwise the benchmark's tests that need them are skipped.

## License

MIT — see [LICENSE](LICENSE).
