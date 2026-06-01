# Routers & Decorators

Two decorators connect your viewset classes to the outside world:

- **`route_viewset`** — registers a viewset on a FastAPI router (HTTP endpoints)
- **`celery_viewset`** — moves a viewset's execution to a Celery worker (transparent task delegation)

Both decorators follow the same class-decoration pattern and can be combined: the same viewset class is decorated with `celery_viewset` in the worker process and with `route_viewset` in the FastAPI process.

---

## `route_viewset`

Collects all routes defined by the mixin hierarchy, resolves generic type parameters, and registers them on a FastAPI `APIRouter`.

### Signature

```python
def route_viewset(
    router: APIRouter,
    base_path: str,
    lifecycle: LifecycleType = "singleton",
    pk_field_name: str | None = None,
) -> Callable[[type], type]:
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `router` | `APIRouter` | — | The FastAPI router to register routes on |
| `base_path` | `str` | — | URL prefix for all endpoints, e.g. `"/items"` |
| `lifecycle` | `LifecycleType` | `"singleton"` | Instance lifecycle — see [Lifecycle modes](#lifecycle-modes) |
| `pk_field_name` | `str \| None` | `None` | Name of the PK field; when set, the field is stripped from the `POST` request body |

### Usage

```python
from fastapi import APIRouter
from fastapi_viewsets.decorators.route_viewset import route_viewset

router = APIRouter()

@route_viewset(router, base_path="/items", pk_field_name="id")
class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item]):
    def __init__(self):
        super().__init__(container=database, pk_field="id")
```

### Lifecycle modes

| Mode | Behaviour |
|------|-----------|
| `"singleton"` | One instance is created when the decorator runs and reused for every request. `load_state()` / `save_state()` are called on every request, so state can be shared across multiple processes (e.g. via Redis). Note: there is currently no locking — concurrent requests may cause race conditions when writing state. |
| `"per-request"` | A new instance is created for every incoming request. `load_state()` / `save_state()` are **not** called. Useful when the viewset needs per-request context (e.g. the current user) with no shared state. |
| `"instance-key"` | A new instance is created per request. `load_state()` is called before the endpoint and `save_state()` after it, so state is loaded fresh and persisted on every request. Same race-condition caveat as `"singleton"` applies. |

### State hooks: `load_state` / `save_state`

`"singleton"` and `"instance-key"` both call two optional async methods on the viewset around each request:

| Method | When called | Purpose |
|--------|-------------|---------|
| `async def load_state(self)` | Before the endpoint handler | Restore state from an external store |
| `async def save_state(self)` | After the endpoint handler (in a `finally` block) | Persist state back to the external store |

Neither method is required — if absent, it is simply skipped. `save_state` is called even if the endpoint raises an exception.

> **Note:** There is currently no distributed locking around `load_state` / `save_state`. In deployments with multiple worker processes, concurrent requests can interleave their load/save cycles and overwrite each other's changes. Add external locking (e.g. a Redis lock) in your `load_state` / `save_state` implementation if you need consistency.

```python
@route_viewset(router, base_path="/session", lifecycle="instance-key")
class SessionViewSet(ListMixin[str]):
    def __init__(self):
        self.items: list[str] = []

    async def load_state(self):
        self.items = await redis.lrange("session:items", 0, -1)

    async def save_state(self):
        await redis.delete("session:items")
        if self.items:
            await redis.rpush("session:items", *self.items)

    async def perform_list(self) -> list[str]:
        return self.items
```

### Automatic OpenAPI tags

The decorator derives an OpenAPI tag from the class name by stripping the `ViewSet` suffix:

- `ItemViewSet` → tag `Item`
- `UserProfileViewSet` → tag `UserProfile`

### Route ordering

Routes are always registered in a consistent order:

1. `/items` — list / create
2. `/items/{pk}` — retrieve / update / destroy
3. `/items/bulk` — bulk operations
4. `/items/lookup` — lookup

### pk_field_name and request body

When `pk_field_name` is set, the decorator automatically removes that field from the Pydantic model used as the `POST` request body. Clients do not need to send the PK when creating a new record — the server assigns it.

---

## `celery_viewset`

Moves a viewset's execution to a Celery worker. The decorator auto-detects the execution context and applies the correct mode:

- **FastAPI process (client mode)** — replaces viewset methods with async wrappers that send Celery tasks and await results via a Redis result queue.
- **Celery worker process (server mode)** — registers each viewset method as a named Celery task that runs the actual implementation.

The same decorator call works in both processes — no conditional code needed in your application.

### Signature

```python
def celery_viewset(
    celery_app: Celery,
    task_prefix: str,
    lifecycle: LifecycleType = "singleton",
    redis_client: redis.Redis | None = None,
) -> Callable[[type], type]:
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `celery_app` | `Celery` | — | The Celery application instance |
| `task_prefix` | `str` | — | Prefix for all registered Celery task names, e.g. `"items"` |
| `lifecycle` | `LifecycleType` | `"singleton"` | Instance lifecycle on the worker side (same semantics as `route_viewset`) |
| `redis_client` | `redis.Redis \| None` | `None` | Redis client used to pass results back to FastAPI. Required in client (FastAPI) mode; optional in worker mode. |

### Usage

The same class definition is used in both processes. The decorator is typically applied in `viewsets.py` and imported by both `main.py` (FastAPI) and `celery_worker.py`:

```python
# viewsets.py
from fastapi_viewsets.decorators.celery_viewset import celery_viewset
from fastapi_viewsets import CollectionViewSet, BulkViewSetMixin

database = {}

@celery_viewset(celery_app, task_prefix="items", redis_client=redis_client)
class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item]):
    def __init__(self):
        super().__init__(container=database, pk_field="id")
```

```python
# main.py (FastAPI) — also applies route_viewset on top
from .viewsets import ItemViewSet
from fastapi_viewsets.decorators.route_viewset import route_viewset

@route_viewset(router, base_path="/items", pk_field_name="id")
class ItemViewSet(ItemViewSet): ...
```

```python
# celery_worker.py — importing viewsets.py is enough; tasks are registered as a side-effect
import myapp.viewsets  # noqa: F401

from myapp.celery_app import celery_app

app = celery_app
```

### Context auto-detection

The decorator detects the execution context by inspecting `sys.argv`:

- If `"celery"` appears in `sys.argv[0]` → **server mode** (worker)
- Otherwise → **client mode** (FastAPI)

For explicit control (e.g. in tests), use `set_is_celery_worker()`:

```python
from fastapi_viewsets.decorators.celery_viewset import set_is_celery_worker

set_is_celery_worker(True)   # force worker mode
set_is_celery_worker(False)  # force client mode
```

### Task naming

Each viewset method is registered as a Celery task named `{task_prefix}.{method_name}`:

| Method | Task name (prefix `"items"`) |
|--------|------------------------------|
| `list` | `items.list` |
| `retrieve` | `items.retrieve` |
| `create` | `items.create` |
| `update` | `items.update` |
| `partial_update` | `items.partial_update` |
| `destroy` | `items.destroy` |
| `bulk_create` | `items.bulk_create` |
| `bulk_update` | `items.bulk_update` |
| `bulk_partial_update` | `items.bulk_partial_update` |
| `bulk_destroy` | `items.bulk_destroy` |

### Result passing

Results are passed from the worker back to FastAPI via a Redis list (not via the Celery result backend). Each call is correlated with a UUID so that concurrent requests are handled correctly.

The result reader background task must be started in the FastAPI lifespan:

```python
from fastapi_viewsets.decorators.celery_viewset import start_result_reader, stop_result_reader

@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_result_reader(redis_client)
    yield
    await stop_result_reader()
```

### Low-level decorators

For explicit control over which mode is applied, use the low-level decorators directly:

| Decorator | Use in |
|-----------|--------|
| `celery_viewset_client` | FastAPI process only |
| `celery_viewset_server` | Celery worker process only |

```python
from fastapi_viewsets.decorators.celery_viewset import celery_viewset_client, celery_viewset_server
```

---

## Combining both decorators

A typical setup uses `celery_viewset` for task delegation and `route_viewset` for HTTP routing. Both are applied to the same class — `celery_viewset` in `viewsets.py` (shared), `route_viewset` in `main.py` (FastAPI only):

```
viewsets.py          ← @celery_viewset  (shared by FastAPI and worker)
main.py              ← @route_viewset   (FastAPI only, wraps the viewset)
celery_worker.py     ← imports viewsets (tasks registered on import)
```

See the [CeleryViewSet guide](./celery-viewset) for a full working example.
