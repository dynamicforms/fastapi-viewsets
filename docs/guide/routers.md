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
| `lifecycle` | `LifecycleType` | `"singleton"` | Instance lifecycle — see [ViewSet Lifecycle](./lifecycle) |
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

### Lifecycle modes and state hooks

`lifecycle` controls how the viewset **class** becomes the **instance** that handles a request
(`"singleton"`, `"per-request"`, or `"instance-key"`), and whether that instance's own
`load_state()`/`save_state()` hooks get called around each request. This is its own dedicated
topic — see [ViewSet Lifecycle](./lifecycle) for the full picture, including the race-condition
caveat and a worked example.

### Response-level side effects: Command Middleware

An action's return value is JSON-serializable data — but sometimes an endpoint needs to affect the response itself
(most commonly: setting a cookie), not just the response body. This matters especially when the action is *also* wired
through [`celery_viewset`](#celery-viewset): the method body may run in a Celery worker, which has no live `Response`
object at all (and its return value must survive a JSON round trip through Redis), so the response-level side effect has
to be applied separately, once the result is back in the FastAPI process.

**Command middleware** is the mechanism for this: a middleware in `settings.viewsets_command_middleware` can
inspect/replace the `ViewSetResult` that `call_next()` returns and set `.headers`/`.cookies` on it - `route_viewset`
applies those onto the real `Response` once the result is back in the FastAPI process, regardless of whether the
action ran in-process or was `celery_viewset`-dispatched to a worker. See the
[Command Middleware guide](./command-middleware) for the full picture.

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
| `lifecycle` | `LifecycleType` | `"singleton"` | Instance lifecycle on the worker side (same semantics as `route_viewset` — see [ViewSet Lifecycle](./lifecycle)) |
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

A result reader background task must be started in the FastAPI lifespan for each `queue_key` (i.e. each distinct `task_prefix` used by a `celery_viewset`-decorated class) — `start_result_reader` keeps one reader task per `queue_key`, so calling it once per prefix is required and sufficient. Every `celery_viewset_client` decorator registers its own `queue_key` at import time (before `lifespan` ever runs), so instead of maintaining your own list of prefixes, iterate `get_registered_queue_keys()` — it can't drift out of sync with your actual viewsets:

```python
from fastapi_viewsets.decorators.celery_viewset import (
    check_result_readers, get_registered_queue_keys, start_result_reader, stop_result_reader,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    for queue_key in get_registered_queue_keys():
        await start_result_reader(redis_client, queue_key)
    check_result_readers()  # logs a warning for any queue_key that still has no running reader
    yield
    await stop_result_reader()  # stops all readers; pass a queue_key to stop just one
```

::: warning One reader per queue_key, not automatic
`start_result_reader(redis_client, queue_key)` starts (and reuses) exactly one reader task per
`queue_key` — calling it for a *new* `queue_key` always starts an additional reader; it never
replaces or interferes with readers for other keys. But nothing starts a reader for a prefix you
never call it for: if your app has more than one `celery_viewset`-decorated class with different
`task_prefix` values (a very common shape) and you forget to start a reader for one of them,
results for it are never picked up — the Celery task runs fine, but the client-side `await` on the
result hangs forever (no exception). `check_result_readers()` is a safety net for exactly this: it
compares every registered `queue_key` against the ones with an actual running reader and logs
`"No result reader is running for queue_key=..."` for each mismatch — call it once, right after
starting your readers in `lifespan`, so a missing reader shows up as a log line at startup instead
of a silent hang on the first request.
:::

### Low-level decorators

For explicit control over which mode is applied, use the low-level decorators directly:

| Decorator | Use in |
|-----------|--------|
| `celery_viewset_client` | FastAPI process only |
| `celery_viewset_server` | Celery worker process only |

```python
from fastapi_viewsets.decorators.celery_viewset import celery_viewset_client, celery_viewset_server
```

### Extension hooks

`celery_viewset_client` and `celery_viewset_server` each expose one module-level hook slot so an
external package can attach dispatch/execute logic (e.g. propagating an operation token, opening a
session around task execution) without this repo depending on that package. Both default to `None`
and are no-ops until set.

`set_celery_dispatch_hook` (client side, in `celery_viewset_client`) registers an
`async (kwargs: dict) -> dict`, awaited just before `send_task` with the call's current kwargs
(a `Context`, if the action declares one, included); its returned dict is merged into the task
kwargs:

```python
from fastapi_viewsets.decorators.celery_viewset import set_celery_dispatch_hook

async def dispatch_hook(kwargs: dict) -> dict:
    return {"_operation_token": current_operation_token()}

set_celery_dispatch_hook(dispatch_hook)
```

`set_celery_kwargs_hook` (worker side, in `celery_viewset_server`) registers a
`(run, kwargs) -> (run, kwargs)` callable, called with the task's raw kwargs before
`_reconstruct_kwargs` turns them into typed arguments — a key the hook doesn't consume would
otherwise reach the action as an argument it never declared. `run` is the callable that executes
the coroutine (`loop.run_until_complete` unless the hook replaces it), letting the hook wrap
execution (e.g. to open a session for the duration of the task):

```python
from fastapi_viewsets.decorators.celery_viewset import set_celery_kwargs_hook

def kwargs_hook(run, kwargs):
    token = kwargs.pop("_operation_token", None)

    def run_with_session(coro):
        with session_scope(token):
            return run(coro)

    return run_with_session, kwargs

set_celery_kwargs_hook(kwargs_hook)
```

---

## Combining both decorators

There are two valid ways to apply `celery_viewset` and `route_viewset` together. Pick based on
whether the FastAPI process needs its own subclass (e.g. to override something FastAPI-only) or
not.

### Pattern 1: subclassing (FastAPI-only overrides)

`celery_viewset` decorates a base class in `viewsets.py` (shared by both processes), and
`route_viewset` decorates a subclass in `main.py` (FastAPI only):

```
viewsets.py          ← @celery_viewset  (shared by FastAPI and worker)
main.py              ← @route_viewset   (FastAPI only, wraps the viewset)
celery_worker.py     ← imports viewsets (tasks registered on import)
```

See `demo/backend/viewsets.py` + `demo/backend/main.py` in the repository for a full working example.

### Pattern 2: stacking on the same class

Both decorators can instead be stacked directly on one class, in a single file imported by both
processes:

```python
@route_viewset(router, base_path="/items", pk_field_name="id")
@celery_viewset(celery_app, task_prefix="items", redis_client=redis_client)
class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item]):
    def __init__(self):
        super().__init__(container=database, pk_field="id")
```

**Order matters: `celery_viewset` must be the inner decorator (closer to the class), `route_viewset`
the outer one**, exactly as written above. Both decorators call an internal, memoizing
`build_schema(cls)` to collect the class's routes; whichever decorator runs *first* is the one that
actually populates it. If `celery_viewset` runs first (correct order), `route_viewset` still
detects its own, FastAPI-specific build hasn't run yet and rebuilds the route table correctly. If
the order is reversed, `celery_viewset`'s own `build_schema(cls)` call is skipped (it sees a route
table already built), so it iterates `route_viewset`'s FastAPI-wrapped routes instead of the raw
viewset methods — including the auto-added `/items/schema` endpoint — which silently registers a
bogus `schema` Celery task and patches a `schema` method onto the class that was never meant to
exist.

Prefer Pattern 1 when the FastAPI-only class needs its own overrides (e.g. extra endpoints, a
different `lifecycle`); prefer Pattern 2 for simple viewsets where a single file is enough — but
respect the decorator order.

---

## Context processors & command middleware

Every standard mixin action (`create`, `list_items`, `retrieve`, ...) also accepts a `context`
parameter (a `Context` instance), built from a global list of processor callables (Django-style
`context_processors`) and forwarded into `perform_*`. This is the recommended way to make
per-request data (e.g. the authenticated user) available to `perform_*` without re-deriving it,
and it survives the `celery_viewset` client/worker boundary. See [Architecture](./architecture) for
how this fits together with the rest of the request pipeline, and
[Context Processors](./context-processors) / [Command Middleware](./command-middleware) for the
two mechanisms themselves.
