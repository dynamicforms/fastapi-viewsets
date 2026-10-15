# Routers & Decorators

Two decorators connect your viewset classes to the outside world:

- **`route_viewset`** — registers a viewset on a FastAPI router (HTTP endpoints)
- **`celery_viewset`** — moves a viewset's execution to a Celery worker (transparent task delegation)

Both decorators follow the same class-decoration pattern and can be combined: the same viewset class is decorated with
`celery_viewset` in the worker process and with `route_viewset` in the FastAPI process.

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
    register_muxws: bool | None = None,
    register_rest: bool | None = None,
) -> Callable[[type], type]:
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `router` | `APIRouter` | — | The FastAPI router to register routes on, for REST and muxws alike; include it in the application |
| `base_path` | `str` | — | URL prefix for all endpoints, e.g. `"/items"` |
| `lifecycle` | `LifecycleType` | `"singleton"` | Instance lifecycle — see [ViewSet Lifecycle](./lifecycle) |
| `pk_field_name` | `str \| None` | `None` | Name of the PK field; when set, a `POST` request body does not carry it — see below |
| `register_muxws` | `bool \| None` | `None` | Publish on the [muxws transport](./muxws); `None` defers to `settings.viewsets_register_muxws` |
| `register_rest` | `bool \| None` | `None` | Publish on REST; `None` means yes |

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

Routes are registered most specific first, so a literal segment is matched before `{pk}` would
take it:

1. `/items/bulk`, `/items/lookup` and other literal paths — bulk operations, lookup, custom endpoints
2. `/items/{pk}` — retrieve / update / destroy
3. `/items` — list / create

Within one path the order is `DELETE`, `PATCH`, `PUT`, `POST`, `GET`.

### pk_field_name and request body

When `pk_field_name` is set, the body of a `POST` route without `{pk}` in its path is a subclass of
the model in which the primary key is not in the JSON schema and is None. The body leaves the
primary key out or gives it as null; any other value answers 422 with `pk_assigned_by_server`. The
server assigns the primary key.

Every other field is validated as the model declares it: its annotation, `Field` constraints and
alias, the model's `model_config` and its validators. The endpoint receives the model itself,
without the primary key unless the model gives it a default; its `model_fields_set` names the fields
the body stated.

A validator of the primary key runs as follows:

- a `@field_validator` of the primary key in `after` mode does not run;
- one in `before`, `wrap` or `plain` mode runs on a value other than null, which is then rejected;
- a `@model_validator` in `after` mode sees the primary key as None.

---

## `celery_viewset`

Moves a viewset's execution to a Celery worker. The decorator auto-detects the execution context and applies the correct
mode:

- **FastAPI process (client mode)** — replaces viewset methods with async wrappers that send Celery tasks and await
  results via a Redis result queue.
- **Celery worker process (server mode)** — registers each viewset method as a named Celery task that runs the actual implementation.

The same decorator call works in both processes — no conditional code needed in your application.

### Signature

```python
def celery_viewset(
    celery_app: Celery,
    task_prefix: str,
    lifecycle: LifecycleType = "singleton",
    redis_client: redis.Redis | None = None,
    result_timeout: float | None = 3600.0,
) -> Callable[[type], type]:
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `celery_app` | `Celery` | — | The Celery application instance |
| `task_prefix` | `str` | — | Prefix for all registered Celery task names, e.g. `"items"` |
| `lifecycle` | `LifecycleType` | `"singleton"` | Instance lifecycle on the worker side (same semantics as `route_viewset` — see [ViewSet Lifecycle](./lifecycle)) |
| `redis_client` | `redis.Redis \| None` | `None` | Redis client used to pass results back to FastAPI. Required in client (FastAPI) mode; optional in worker mode. |
| `result_timeout` | `float \| None` | `3600.0` | Client mode: seconds a call waits for its result before it raises `ResultTimeoutError` (504, `result_timeout`). `None` waits without a limit. Ignored in worker mode. |

### Usage

The same class definition is used in both processes. The decorator is typically applied in `viewsets.py` and imported by
both `main.py` (FastAPI) and `celery_worker.py`:

```python
# viewsets.py
from fastapi_viewsets.decorators.celery_viewset import celery_viewset
from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.mixins import BulkViewSetMixin

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

Each route method is registered as a Celery task named `{task_prefix}.{route_name}`, the route's
name, which is the method's name unless the route states one:

| Method | Task name (prefix `"items"`) |
|--------|------------------------------|
| `list_items` | `items.list_items` |
| `retrieve` | `items.retrieve` |
| `create` | `items.create` |
| `update` | `items.update` |
| `partial_update` | `items.partial_update` |
| `destroy` | `items.destroy` |
| `bulk_create` | `items.bulk_create` |
| `bulk_update` | `items.bulk_update` |
| `bulk_partial_update` | `items.bulk_partial_update` |
| `bulk_destroy` | `items.bulk_destroy` |
| `lookup` | `items.lookup` |

### Result passing

Results are passed from the worker back to FastAPI via a Redis list (not via the Celery result backend). Each call is
correlated with a UUID so that concurrent requests are handled correctly.

Each FastAPI process receives its results on its own list, `{queue_key}:{host name}:{process id}`
(`get_process_queue_key(queue_key)`): the task carries the list of the process that dispatched it,
and that process's reader pops it. Any number of FastAPI processes, on any number of hosts, can share
one Redis. Every push renews the list's expiry to one hour, so the list of a process that has exited
is removed from Redis.

A call raises `ResultTimeoutError` (504, `result_timeout`) when its result does not arrive within
`result_timeout` seconds, one hour by default.

A call pending when `stop_result_reader` stops the reader of its `queue_key`, or when that reader task
is cancelled, raises `ResultReaderStoppedError` (503, `result_reader_stopped`, param `queue_key`). A
cancelled call removes its pending entry; its result, if the worker still sends one, is logged as
having no pending call and dropped. `send_task` runs in a worker thread, so a slow broker does not
block the event loop.

A model argument crosses to the worker dumped by field name and is validated as its model there. A
create body crosses without its primary key, and the action receives the model as it does in the
FastAPI process. A model argument that fails validation on the worker fails the call.

A result reader background task must be started in the FastAPI lifespan for each `queue_key` (i.e. each distinct
`task_prefix` used by a `celery_viewset`-decorated class) — `start_result_reader` keeps one reader task per `queue_key`,
so calling it once per prefix is required and sufficient. Every `celery_viewset_client` decorator registers its own
`queue_key` at import time (before `lifespan` ever runs), so instead of maintaining your own list of prefixes, iterate
`get_registered_queue_keys()` — it can't drift out of sync with your actual viewsets:

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
result waits until `result_timeout` passes and raises `ResultTimeoutError`. `check_result_readers()`
is a safety net for exactly this: it compares every registered `queue_key` against the ones with an
actual running reader and logs `"No result reader is running for queue_key=..."` for each mismatch —
call it once, right after starting your readers in `lifespan`, so a missing reader shows up as a log
line at startup instead of as timed-out requests.
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

### Result queue helpers

| Function | Description |
|----------|-------------|
| `get_result_queue_key(task_prefix)` | The queue key of a prefix, which `start_result_reader` takes |
| `get_process_queue_key(queue_key)` | The Redis list this process receives a queue key's results on |
| `get_registered_queue_keys()` | Every queue key a `celery_viewset_client` registered |
| `get_running_queue_keys()` | The queue keys that have a running reader task |
| `push_result(redis_client, queue_key, correlation_id, result=None, error=None)` | Pushes a result or an error for one call to `queue_key`, the `_result_queue_key` the task carries, and renews the list's expiry |

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

### Execution hook

```python
ExecutionHook = Callable[[ActionExecution], AbstractAsyncContextManager[Any]]

def set_execution_hook(hook: ExecutionHook | None) -> None: ...
```

`set_execution_hook` (in `fastapi_viewsets.decorators`, with `ActionExecution` and
`ExecutionHook`) registers a callable that receives an `ActionExecution` and returns an async
context manager. `lifecycle_runner` enters it right before
the viewset method executes and exits it when the method has returned or raised; `None` removes it.
It runs where the action executes:

| Action | Where the hook runs |
|--------|---------------------|
| in-process (`route_viewset` only) | the FastAPI process, inside the command middleware chain |
| `celery_viewset` | the Celery worker, in `celery_viewset_server`; not in the FastAPI process, whose method only dispatches the call |

`ActionExecution` is a frozen dataclass:

| Field | Type | Description |
|-------|------|-------------|
| `viewset_class` | `type` | The viewset class |
| `action` | `str` | The name of the viewset method, e.g. `"create"` |
| `context` | `Context \| None` | The call's `Context`; `None` in a Celery worker for an action that does not declare a `context` parameter |
| `request` | `Request \| None` | The live request; `None` in a Celery worker |

```python
from contextlib import asynccontextmanager

from fastapi_viewsets.decorators import set_execution_hook

@asynccontextmanager
async def execution_hook(execution):
    await mark_running(execution.action)
    try:
        yield
    except Exception:
        await mark_failed(execution.action)
        raise
    await mark_done(execution.action)

set_execution_hook(execution_hook)
```

The methods `celery_viewset_client` installs carry the attribute named by
`fastapi_viewsets.decorators.lifecycle_runner.CELERY_DISPATCH_ATTRIBUTE` (`"__celery_viewset_dispatch__"`),
and `lifecycle_runner` does not enter the hook around them. A FastAPI-side subclass that overrides
such a method and calls it through `super()` is an in-process method: the hook runs for it in the
FastAPI process, and again in the worker.

An exception the method raises reaches the context manager's `__aexit__` and propagates after it. An
exception raised on entry keeps the method from running; one raised on exit propagates in place of
the method's outcome.

---

## Combining both decorators

There are two valid ways to apply `celery_viewset` and `route_viewset` together. Pick based on
whether the FastAPI process needs its own subclass (e.g. to override something FastAPI-only) or
not.

### Pattern 1: subclassing (FastAPI-only overrides)

`celery_viewset` decorates a base class in `viewsets.py` (shared by both processes), and
`route_viewset` decorates a subclass in `main.py` (FastAPI only):

```text
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
