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

A viewset whose data lives in process memory, such as a [`CollectionViewSet`](./collection-viewset#one-copy-per-process),
has one copy of it per worker process.

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
| `redis_client` | `redis.Redis \| None` | `None` | Redis client used to pass results back to FastAPI. Required in both modes; without it the decorator raises `ValueError`. |
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
`result_timeout` seconds, one hour by default. Every outcome the worker observes reaches the caller
as soon as it happens (see [Outcomes of a call](#outcomes-of-a-call)); `result_timeout` covers what
the worker cannot observe: a supervisor process that is itself killed, or a task message that
cannot be decoded.

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
| `push_result(redis_client, queue_key, correlation_id, result=None, error=None, task=None)` | Pushes a result or an error for one call to `queue_key`, the `_result_queue_key` the task carries, and renews the list's expiry. An `HTTPException` error reaches the caller as it is; any other error as `ActionFailedError` naming `task` |

### Call data in the worker

Every task carries, beside the action's own arguments, the call's data under reserved kwargs:

| Kwarg | Content |
|-------|---------|
| `_context` | The call's context, serialized whether or not the action declares a `context` parameter. A `LazyObject` crosses as its recipe, resolved only where it is awaited |
| `_request` | The call's ASGI scope without the body: method, path, query string, headers, path parameters, client and server address |
| `_parent` | The `ExecutionRef` of the execution in which the call was dispatched |
| `_correlation_id`, `_result_queue_key` | Where the result goes (see [Result passing](#result-passing)) |

The worker consumes them before the action is called. An action that declares `context` receives
the call's context, and one that declares `request` receives a `Request` built from the scope,
which has no body, no `app` and no `state`. [`Middleware.execute()`](./command-middleware#around-the-execution-middleware-execute)
receives both either way.

A call made from inside another action's execution (an action calling a `celery_viewset` method)
carries that execution's context and request, unless it passes a `context` of its own, and that
execution as its `parent`. A call made outside any call carries an empty context and no request.

A call whose arguments cannot be serialized raises `ActionDispatchError` (503,
`action_dispatch_failed`, reason `serialization`), and a call whose task cannot be sent raises it with
reason `send`; the error that prevented the dispatch is its `__cause__`.

### Outcomes of a call

No Celery, billiard or kombu exception reaches the caller, `Middleware.execute()` or the action's
own `except` clauses at the points listed below; each outcome arrives as one of this package's
errors (see [Error Codes](./error-codes#executor-outcomes)):

| What happens | Caller receives | Observed in the worker by |
|---|---|---|
| The action returns | its result | `execute()` |
| The action raises an `HTTPException` | that exception: a `DfViewSetError` of a class `fastapi_viewsets.exceptions` defines keeps its class, `code`, `params` and headers, any other one is rebuilt as `HTTPException` or `DfViewSetError` with the same fields | `execute()` |
| The action raises anything else (a Celery `Retry`, `Ignore` or `Reject` included), or a model argument fails validation in the worker | `ActionFailedError` (500, reason `error`); the original error is logged in the worker | `execute()` |
| The result cannot be serialized, or deserialized by the caller | `ActionFailedError` (500, reason `result`) | — |
| Soft time limit | `ActionInterruptedError` (504, reason `time_limit`) | `execute()` (`ActionInterruptedError` from `call_next()`) |
| Hard time limit | `ActionInterruptedError` (504, reason `time_limit`) | `on_transition("interrupted")`; in a `gevent` pool also `execute()` (`ActionInterruptedError` from `call_next()`) |
| `revoke(..., terminate=True)` | `ActionInterruptedError` (503, reason `terminated`) | `on_transition("interrupted")` |
| The worker process exits | `ActionInterruptedError` (503, reason `lost`) | `on_transition("interrupted")` |
| Revoked or expired before it started | `ActionDispatchError` (503, reason `revoked` / `expired`) | `on_transition("not_run")` |
| The worker has no task of that name under a registered `task_prefix` | `ActionDispatchError` (503, reason `unknown_task`) | `on_transition("not_run")` |
| The worker rejects the message | `ActionDispatchError` (503, reason `rejected`) | `on_transition("not_run")` |
| The worker's own machinery fails, or `executor_started()` raises | `ActionFailedError` (500, reason `executor`) | `on_transition("failed")` / — |
| The caller stops waiting | `ResultTimeoutError` / `ResultReaderStoppedError`, both `ActionOutcomeUnknownError` | — |

A hard time limit, a termination and a lost process end the process executing the action. Celery
stops it with SIGTERM: the execution unwinds as `SystemExit` through `finally` blocks and
`execute()` only as far as it gets before the process is killed, so `save_state()` is not
guaranteed to run. The supervisor process sends the caller its error and then calls
[`on_transition()`](./command-middleware#transitions-outside-the-execution-middleware-on-transition).

In a `gevent` pool a hard time limit ends the execution's greenlet, not the process. Celery raises
`gevent.Timeout` in it: the action sees that exception where it runs synchronously and
`asyncio.CancelledError` where it is suspended at an `await`, `execute()` sees
`ActionInterruptedError` from `call_next()`, and `finally` blocks and `save_state()` run. The worker
process then sends the caller its error and calls `on_transition("interrupted")`. The time limit
interrupts only code that yields to gevent; synchronous code that does not runs on.

Celery's `current_task.retry()` publishes the retry before it raises, so the task runs again while
its caller has already received `ActionFailedError`; the second run's result is dropped. A
`celery_viewset` action does not retry through Celery.

The tasks are registered with `ignore_result=True`, `acks_late=False`,
`reject_on_worker_lost=False` and `store_errors_even_if_ignored=False`, which override a global
`task_acks_late`: a call is acknowledged when its execution starts and is never delivered to the
worker a second time, since its caller receives one outcome only.

### Time limits

An action's time limits are configured with
[`@action_configuration`](./action-configuration#time-limits-of-a-deferred-action) under the
`TimeLimits` key, not in Celery's configuration; the client sends them with each call as the task's
`soft_time_limit` and `time_limit`:

```python
from fastapi_viewsets.action_configuration import action_configuration, TimeLimits

@celery_viewset(celery_app, task_prefix="reports", redis_client=redis_client)
class ReportViewSet(...):
    @action_configuration({TimeLimits: TimeLimits(soft=30, hard=60)})
    async def perform_create(self, data: Report) -> Report: ...
```

`settings.default_action_configuration = {TimeLimits: TimeLimits(...)}` sets them for every action.
An action without a `TimeLimits` entry runs under the worker's own limits (`--soft-time-limit`,
`--time-limit`). Soft time limits need the `prefork` pool, hard time limits the `prefork` or
`gevent` pool; the `solo`, `threads` and `eventlet` pools enforce neither. An action executed in the
FastAPI process has no time limit.

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
