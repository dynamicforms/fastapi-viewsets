# ViewSet Lifecycle

Controls how a viewset **class** becomes the actual **instance** that handles a request, and
whether/how that instance's own state persists across requests.

::: tip Where this fits
Lifecycle is orthogonal to the request pipeline described in [Architecture](./architecture):
`context` (built by [Context Processors](./context-processors)) is about *who's calling*, this page
is about *which viewset instance* handles the call and what it remembers between calls. Don't
confuse the two - a viewset's own state (e.g. a cache, a counter, an open connection) is not the
same thing as per-request context.
:::

---

## The `lifecycle` parameter

Both `route_viewset` and `celery_viewset` (see [Routers & Decorators](./routers)) accept a
`lifecycle: LifecycleType = "singleton"` parameter with identical semantics on both the FastAPI and
the worker side:

| Mode | Instance creation | State hooks |
|------|-------------------|-------------|
| `"singleton"` | One instance is created when the decorator runs and reused for every request. | `load_state()` / `save_state()` are called on every request, so state can be shared across multiple processes (e.g. via Redis). No locking — concurrent requests may race when writing state. |
| `"per-request"` | A new instance is created for every incoming request. | Not called. Useful when the viewset needs per-request data (e.g. the current user - see [Context Processors](./context-processors)) with no shared state. |
| `"instance-key"` | A new instance is created per request. | `load_state()` is called before the endpoint and `save_state()` after it, so state is loaded fresh and persisted on every request. Same race-condition caveat as `"singleton"`. |

## State hooks: `load_state` / `save_state`

`"singleton"` and `"instance-key"` both call two optional async methods on the viewset around each
request:

| Method | When called | Purpose |
|--------|-------------|---------|
| `async def load_state(self)` | Before the endpoint handler | Restore state from an external store |
| `async def save_state(self)` | After the endpoint handler (in a `finally` block) | Persist state back to the external store |

Neither method is required — if absent, it is simply skipped. `save_state` is called even if the
endpoint raises an exception.

> **Note:** There is currently no distributed locking around `load_state` / `save_state`. In
> deployments with multiple worker processes, concurrent requests can interleave their load/save
> cycles and overwrite each other's changes. Add external locking (e.g. a Redis lock) in your
> `load_state` / `save_state` implementation if you need consistency.

```python
from fastapi_viewsets.context import Context

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

    async def perform_list(self, context: Context) -> list[str]:
        return self.items
```

## Where this runs relative to the rest of the pipeline

`load_state()` runs before context is built and before the command middleware chain starts;
`save_state()` runs after command middleware unwinds, in a `finally` block, so it always runs even
if the endpoint or a middleware raises. See the full ordering in [Architecture](./architecture#the-request-pipeline).

## Built-in Redis-backed state: `SaveStateRedis`

`fastapi_viewsets.save_state` ships a ready-made `load_state`/`save_state` implementation backed by
Redis, if you don't want to write your own:

```python
from fastapi_viewsets.save_state.save_state import SaveStateRedis

@route_viewset(router, base_path="/session", lifecycle="instance-key")
class SessionViewSet(SaveStateRedis, ListMixin[str]):
    save_state_redis_key = "session:items"  # required class variable

    def __init__(self, redis):
        super().__init__(instance_id="session", redis=redis)
        self.items: list[str] = []
```

`SaveStateRedis(instance_id, redis)` reads/writes `save_state_redis_key` via `redis.get`/`redis.set`,
serializing through `serialize_state()`/`deserialize_state()` (from the `SerializeState` base it
also inherits). `SerializeStateSlots` is a ready-made `serialize_state`/`deserialize_state` pair for
classes that declare `__slots__`: it JSON-dumps/loads exactly those slots, optionally through a
`custom_json_encoder`/`custom_json_decoder` pair (the same pattern used by
`settings.viewsets_context_json_encoder`/`decoder` - see [Context Processors](./context-processors)).

## Known limitations

- `load_state`/`save_state` are a separate mechanism from [Context Processors](./context-processors)
  and [Command Middleware](./command-middleware) - neither of those replace it, and there's
  currently no plan to fold them together (see the "why two separate mechanisms" reasoning in
  [Architecture](./architecture#why-two-separate-mechanisms-not-one), which applies here too).
