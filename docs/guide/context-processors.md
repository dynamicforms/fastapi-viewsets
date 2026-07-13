# Context Processors

A global list of callables that build a per-request `context`, injected into every standard CRUD
endpoint (`create`, `list_items`, `retrieve`, ...) and forwarded from there into the corresponding
`perform_*` method. The canonical use case is authentication - resolve the current user once, and
have it available in every `perform_*` implementation without re-deriving it.

::: tip Where this fits
Context processors are one stage in the request pipeline - see [Architecture](./architecture) for
how they relate to `route_viewset`, [Command Middleware](./command-middleware), and the actual
`perform_*` execution.
:::

---

## Configuring processors

Processors are configured on the global `settings` object:

```python
from fastapi import Request
from fastapi_viewsets.conf import settings

async def auth_processor(request: Request, viewset) -> dict:
    user = await authenticate(request)
    return {"user": user}

settings.viewsets_context_processors = [auth_processor]
```

Each processor is `async def processor(request: Request, viewset) -> dict`. All configured
processors run for every request that needs a context (see below), in list order, and their
returned dicts are merged - later processors win on key collisions.

::: tip Not a middleware chain
This mirrors Django's *template* `context_processors` (a flat list whose dicts get merged), not
Django *middleware* (`get_response()`-style request/response wrapping). Processors don't call each
other, don't call the endpoint themselves, and never see its return value - they only run once,
before the endpoint, to build `context`. Mutating the HTTP response itself is a separate mechanism -
see [Command Middleware](./command-middleware).
:::

## Receiving context in your viewset

`context` is a normal parameter (a `Context` instance) on every mixin action - declare it as the
first parameter after `self` on `perform_*`:

```python
from fastapi_viewsets.context import Context
from fastapi_viewsets.collection_viewset import CollectionViewSet

class ItemViewSet(CollectionViewSet[int, Item]):
    async def perform_list(self, context: Context) -> list[Item]:
        user = await context.user
        ...
```

### `Context`: uniform, awaitable field access

`context.user` and `context["user"]` both work, and **always return something awaitable** -
whether the underlying value is a plain eager value or a lazily-resolved `LazyObject` (see below),
callers never need to know or branch on which:

```python
async def perform_list(self, context: Context) -> list[Item]:
    tz = await context.timezone   # a plain value, resolves "instantly"
    user = await context.user     # a LazyObject, actually does the work here
```

This is the closest achievable equivalent to Django's `SimpleLazyObject` transparency. True
zero-`await` transparency (`context.user.username` with no `await` anywhere) is impossible when
resolution needs real I/O: `__getattr__` is a synchronous protocol method, so it can't itself
await anything. Django hit the exact same wall with async views and added `request.auser()` as an
explicit async accessor rather than trying to make `request.user` transparently async - `context`
follows the same pattern: one explicit `await`, and from then on you're holding the real object.

### Custom endpoints

Hand-written `__router` endpoints (see [Custom Endpoints](./custom-endpoints)) can opt into the
same mechanism by declaring `context` themselves:

```python
class ItemViewSet(CollectionViewSet[int, Item]):
    __router = APIRouter()

    @__router.get("mine")
    async def mine(self, context: Context) -> list[Item]:
        user = await context.user
        return [i for i in await self.perform_list(context) if i.owner == user]
```

A route that does **not** declare a `context` parameter is entirely unaffected - no `Request` is
injected, no processors run, exactly as if the feature didn't exist. (Detection is by parameter
*name* - `context` - not by type.)

## Where context is built

Context is built **once**, centrally, by `route_viewset` right before the endpoint runs - not by
a decorator on each mixin method. This matters because a viewset's execution can be transparently
delegated to a Celery worker (see the `celery_viewset` section in
[Routers & Decorators](./routers)): the worker process has no live `Request` to build a context
from, so the context has to be built in the FastAPI process (which has the `Request`) and carried
across the Celery/Redis boundary to the worker.

## Custom (de)serialization: `SerializableObject` and `LazyObject`

Most values in context (strings, numbers, plain JSON-safe data) travel across the Celery/Redis
boundary without any help. Some values need more - either because reconstructing them requires
domain-specific logic, or because you don't want to eagerly compute them at all unless something
actually uses them. For those, `fastapi_viewsets.context` provides two base classes.

### `SerializableObject`

A base class for a context *value* (not the context dict itself) that knows how to serialize and
rebuild itself:

```python
from typing import Any
from fastapi_viewsets.context import SerializableObject

class Money(SerializableObject):
    def __serialize__(self) -> Any:
        return {"cents": self.value.cents, "currency": self.value.currency}

    @classmethod
    def __deserialize__(cls, data: Any) -> "Money":
        return cls(SomeMoneyType(data["cents"], data["currency"]))
```

`value` holds the actual wrapped value, and the instance itself is awaitable (`await context.price`
works the same as for any other field, even though nothing here is actually lazy). `serialize_context()`/
`deserialize_context()` (used internally by `celery_viewset_client`/`celery_viewset_server`) detect
`SerializableObject` instances in the context dict via `isinstance` and swap them for a tagged,
JSON-safe payload:

```python
{"__fpv_type__": "yourapp.money.Money", "__fpv_value__": {"cents": 500, "currency": "EUR"}}
```

The `__fpv_type__` tag is the class's dotted import path (`module.QualName`) - deserialization
imports that exact module and looks up the class with a plain `getattr`, no separate registry
required. The keys are prefixed (rather than the plainer `__type__`/`__value__`) to avoid
colliding with `kombu.utils.json`'s own `__type__`/`__value__` convention for its JSON codec -
without the prefix, a Celery worker's `object_hook` would intercept this payload before it ever
reached `deserialize_context()`.

::: warning
`SerializableObject` subclasses must be importable at module level. A class defined inside a
function (its `__qualname__` would contain `<locals>`) can't be re-imported this way.
:::

### `LazyObject`

A `SerializableObject` whose value is computed lazily via `resolve()` - only if and when something
actually awaits it (e.g. never, if a particular `perform_*` override doesn't touch it), and only
once (the result is memoized; concurrent awaiters share the same underlying resolution rather than
each independently triggering it). `resolve()` supports two modes, and callers don't need to know
which one a given subclass uses:

- **Sync** - `resolve()` returns the value directly.
- **Async** - `resolve()` returns an awaitable - the common way to do this is to just return the
  coroutine from an `async def` helper without awaiting it (`return self._fetch()`); a
  `Future`/`Task` works too.

Either way, `await obj` (or `await context.field`) gives you the actual resolved value directly -
not a wrapper - so from that point on it behaves exactly like the real object. `.value` is a
synchronous peek at an already-resolved value (raises `RuntimeError` if nothing has awaited the
object yet); `.is_resolved` tells you whether that's the case.

Crucially, `serialize_recipe()` should carry whatever's needed to resolve the value again later -
not the resolved value itself - so resolution can happen wherever/whenever it's actually needed
(the FastAPI process, a Celery worker, or never at all):

```python
from typing import Any
from fastapi_viewsets.context import LazyObject

class SessionUser(LazyObject):
    def __init__(self, session_id: str):
        super().__init__()
        self.session_id = session_id

    def serialize_recipe(self) -> Any:
        return {"session_id": self.session_id}  # cheap - not the resolved user

    @classmethod
    def deserialize_recipe(cls, data: Any) -> "SessionUser":
        return cls(data["session_id"])

    def resolve(self) -> Any:
        return self._fetch_user()  # a coroutine - `await obj` will await it

    async def _fetch_user(self) -> Any:
        session = await read_session(self.session_id)
        return await load_user(session.user_id)
```

```python
async def auth_processor(request, viewset) -> dict:
    return {"user": SessionUser(request.cookies.get("sessionid", ""))}

settings.viewsets_context_processors = [auth_processor]
```

```python
async def perform_list(self, context: Context) -> list[Item]:
    user = await context.user
    return [i for i in await self.container_list() if i.owner == user]
```

If the action is `celery_viewset`-dispatched, the worker receives an *unresolved* `SessionUser`
(just the `session_id`) and resolves it itself, on its own side of the boundary, only if the
`perform_*` implementation actually awaits it. If it was **already** resolved before serialization
(e.g. the FastAPI process already needed it), the resolved value travels alongside the recipe as a
cheap shortcut, so the worker doesn't redundantly re-resolve something already known - while still
getting its own, fully independent instance.

::: warning `SerializableObject`/`LazyObject` construction must be loop-independent
`__init__`/`__deserialize__`/`deserialize_recipe` must never create real `asyncio` objects (a
`Future`/`Task`) - reconstruction happens synchronously in `celery_viewset_server`'s
`_reconstruct_kwargs`, before the worker's event loop starts. `LazyObject`'s own implementation
only creates its internal `Future` lazily, on first `await`, which is always guaranteed to run
inside a live event loop.
:::

## Non-`SerializableObject` values that aren't JSON-native

For plain values that aren't natively JSON-safe (e.g. a raw `datetime`) but don't need the full
`SerializableObject` machinery, configure a custom encoder/decoder pair instead - the same pattern
used by [`SerializeStateSlots`](./lifecycle#built-in-redis-backed-state-savestateredis)'s
`custom_json_encoder`/`custom_json_decoder`:

```python
import json, datetime
from fastapi_viewsets.conf import settings

class DTEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, datetime.datetime):
            return {"__dt__": o.isoformat()}
        return super().default(o)

class DTDecoder(json.JSONDecoder):
    def __init__(self, *a, **kw):
        super().__init__(*a, object_hook=self._hook, **kw)

    @staticmethod
    def _hook(d):
        if "__dt__" in d:
            return datetime.datetime.fromisoformat(d["__dt__"])
        return d

settings.viewsets_context_json_encoder = DTEncoder
settings.viewsets_context_json_decoder = DTDecoder
```

## Known limitations

- `context` is threaded through the 11 standard router methods and their `perform_*`
  counterparts. It is **not** threaded through the secondary hooks `setup_filter`/`filter_list`/
  `setup_sort`/`sort_list`/`setup_lookup_filter`/`filter_lookup`.
- `load_state`/`save_state` (see [ViewSet Lifecycle](./lifecycle)) remain a separate mechanism -
  context processors don't replace them.
