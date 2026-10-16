# Architecture

This page describes the request pipeline piece by piece, on one example: an `Item` viewset that
needs authentication. Each piece has its own reference page: [Context Processors](./context-processors),
[Command Middleware](./command-middleware), [Action Configuration](./action-configuration),
[Authentication](./authentication), [Authorization](./authorization),
[ViewSet Lifecycle](./lifecycle) and [Routers & Decorators](./routers).

---

## Start minimal

```python
database: dict[int, Item] = {1: Item(id=1, name="Widget")}

@route_viewset(router, base_path="/items", pk_field_name="id")
class ItemViewSet(CollectionViewSet[int, Item], ViewSetMixin[int, Item]):
    def __init__(self):
        super().__init__(container=database, pk_field="id")
```

That serves `GET /items`, `POST /items`, `GET`/`PUT`/`PATCH`/`DELETE /items/{pk}`. No middleware, no
lifecycle concerns - `ViewSetMixin` declares the routes, `route_viewset` registers them, and
`CollectionViewSet` implements every `perform_*` against the in-memory `database` dict. The
sections below add one requirement each and the mechanism that meets it.

## Requirement: the caller's identity

`ItemViewSet` filters results by the caller. Every `perform_*` takes a `context` parameter, which
carries per-request facts:

```python
class ItemViewSet(CollectionViewSet[int, Item]):
    async def perform_list(self, context: Context) -> list[Item]:
        user = await context.user
        return [i for i in database.values() if i.owner == user]
```

A **context processor** populates `context.user` from the live `Request` before `perform_list`
runs:

```python
async def whoami_processor(request: Request, viewset) -> dict:
    return {"user": request.headers.get("x-user")}

settings.viewsets_context_processors = [whoami_processor]
```

`route_viewset` builds the context once per request by running every configured processor and
merging their dicts; the route method (`list_items`) passes it to `perform_list`. `context.user` is
always awaitable, also where the value is a plain string, because a value can be resolved lazily
(next section). → full depth in [Context Processors](./context-processors).

## Requirement: several credential schemes

An application may accept an `X-Session-Token` against a fixed list of users, a Django session, or
both, tried in order until one recognizes the request. Each credential scheme is a **backend**: its
`try_handle(request)` returns `None` ("not mine, try the next one") or claims the request and
returns an object that resolves to the user later:

```python
class AuthBackend(ABC):
    @abstractmethod
    def try_handle(self, request: Request) -> LazyObject | None: ...

settings.viewsets_auth_processors = [StaticUserAuthBackend(...), DjangoSessionAuthBackend()]
```

and one small, reusable context processor - `auth_context_processor` - iterates them:

```python
async def auth_context_processor(request: Request, viewset) -> dict:
    for backend in settings.viewsets_auth_processors:
        lazy_user = backend.try_handle(request)
        if lazy_user is not None:
            return {"user": lazy_user}
    return {"user": None}

settings.viewsets_context_processors = [auth_context_processor]
```

`try_handle` returns a `LazyObject`, not the user: resolving a Django session is a DB/cache lookup,
and it runs only when `perform_*` awaits `context.user`. Across the Celery/Redis boundary of a
`celery_viewset`-dispatched action, the *recipe* (a session key) travels, not a live database
object, and the worker resolves it on its own side. `LazyObject` (a `SerializableObject` subclass
with lazy, memoized resolution) makes `context.user` awaitable and (de)serializable regardless of
which backend produced it. → full depth in
[Authentication](./authentication) and the `LazyObject`/`SerializableObject` section of
[Context Processors](./context-processors).

## Requirement: rejecting a request

An expired or missing session fails the request with `401` before `perform_list` runs. Context
processors cannot do this: they are a **flat list** (Django *template* `context_processors` style).
Each returns a dict and the dicts are merged; none calls the endpoint, sees its result or stops the
others.

**Command middleware** is an onion chain wrapping the execution, each layer calling `call_next()`
for the next one. A middleware is any callable with the right signature - a plain function, or a
class implementing `Middleware.__call__`.

A check-and-reject middleware such as `Session` has no "after" phase. `Middleware` has an optional
second method, `depends()`, that `route_viewset` bridges onto FastAPI's `Depends()`, so it runs
before FastAPI parses the request body:

```python
class Session(Middleware):
    async def depends(self, request, cls, context):
        config = self.config_from(context)
        required = True if config is None else bool(config)
        if not required:
            return
        user = await context.user
        if user is None:
            raise SessionExpiredError()

    async def __call__(self, request, viewset, context, call_next):
        return await call_next()   # depends() makes the decision

settings.viewsets_command_middleware = [Session()]
```

An `HTTPException` raised inside `depends()` propagates out through FastAPI's dependency
resolution; `perform_list`, the onion chain and request-body parsing do not run. The status is 401,
not 403: a missing, malformed or expired credential does not establish who is calling. A viewset
that must stay reachable without a session (a login endpoint) opts out with
`@action_configuration({Session: False})`, which `Session` reads with `self.config_from(context)`.
→ full depth in [Command Middleware](./command-middleware) and
[Authentication](./authentication).

The context processor gathers the fact ("user is `None`"); the command middleware decides what to
do about it. Whether the decision runs early, in `depends()`, or in the onion chain, it is command
middleware either way.

## Requirement: per-viewset configuration

Middleware and processors need per-viewset settings: `Session`'s opt-out, a rate limit that is
5/minute on one viewset and 100/minute on another. A bespoke class attribute per concern
(`requires_auth = False`, `rate_limit = 5`), each read with its own `getattr(...)`, is a separate
convention for every processor and middleware.

`@action_configuration({...})` is one mechanism for all of them: a decorator (usable on a viewset
class or an individual method) carrying a dict keyed by whichever
context processor or `Middleware` cares - the value is opaque, interpreted only by that
identifier's own code. `Session.config_from(context)`, shown above, is shorthand for
`context.configuration_for(type(self))`, which resolves the opt-out:

```python
from fastapi_viewsets.action_configuration import action_configuration

@action_configuration({Session: False, RateLimiter: 5})
class PublicButThrottledViewSet(...): ...
```

Configuration merges per identifier across three layers - `settings.default_action_configuration`
(global fallback) → the viewset class's `@action_configuration` → an individual method's
`@action_configuration` (highest priority, overriding just its own identifiers - other identifiers
the method doesn't mention still fall through from the class). A value can vary by action
(`ByAction(list_items="read", update="write")`), resolved against the running action. → full depth
in [Action Configuration](./action-configuration), and see
[Rate Limiter](./rate-limiter) and [Authorization](./authorization) for `RateLimiter` and
`Authorization`, two real middleware shipped with this library that build on it.

## Requirement: viewset state across requests

A viewset instance may keep state across requests - a cache, a counter, an open connection -
independent of the caller. This is neither context (a per-request fact about the caller) nor
command middleware (a step around execution); it is controlled by the `lifecycle` parameter and two
optional hooks:

```python
@route_viewset(router, base_path="/session", lifecycle="instance-key")
class SessionViewSet(ListMixin[str]):
    async def load_state(self):
        self.items = await redis.lrange("session:items", 0, -1)

    async def save_state(self):
        await redis.rpush("session:items", *self.items)
```

`load_state()` runs before the context is built; `save_state()` runs after the command middleware
chain unwinds, in a `finally` block, so it also runs when something above raised. → full depth in
[ViewSet Lifecycle](./lifecycle).

## Requirement: background execution

A slow `perform_list` runs on a Celery worker. `celery_viewset` does this transparently: the same
`ItemViewSet` class, decorated once more, dispatches its route methods (`list_items`, `create`, …)
to a worker as Celery tasks and awaits the result over Redis instead of running them in-process; the
worker runs the route method, which calls `perform_*` there.

`context` crosses to the worker, which has no live `Request` to build one from.
`serialize_context`/`deserialize_context` carry it: plain values cross as-is, a `SerializableObject`
as a tagged JSON payload, and a `LazyObject` (such as `context.user`) as its unresolved *recipe*,
which the worker resolves only if the worker-side `perform_list` awaits it. The auth backends need
no Celery-specific code. → full depth in [Routers & Decorators](./routers).

## The request pipeline

Pulling every piece from the sections above into one picture, for a single HTTP request, in order:

```text
HTTP request
     │
     ▼
FastAPI's own Depends() resolution (before FastAPI parses the request body)
     │      Middleware.depends(), for every configured command middleware that defines it
     │      (e.g. Session/Authorization/RateLimiter can short-circuit here with a 401/403/429 -
     │       this is also where Context gets built, cached for reuse below - see Context
     │       Processors / Authentication)
     ▼
FastAPI parses/validates the request body (422 on failure) - only reached if depends() allowed it
     │
     ▼
route_viewset-registered endpoint (lifecycle: singleton / per-request / instance-key)
     │
     ├─ if lifecycle uses state:  await viewset.load_state()
     │
     ├─ Context: reuse the one built in the Depends() phase above (or build one now, running
     │      settings.viewsets_context_processors, if depends() never touched it); passed as
     │      `context` to an endpoint that declares it
     │
     ▼
Command middleware chain (settings.viewsets_command_middleware) — before phase, in list order
     │      (a middleware whose entire job lives in depends() only calls call_next() here)
     ▼
Middleware.execute() chain, around the viewset method, then perform_* execution
     │      (optionally delegated: celery_viewset ships `context` to a worker, where the
     │       execute() chain and the method run, awaits the result over Redis, and returns
     │       transparently)
     │
     ▼
Command middleware chain — after phase, unwinding in reverse order
     │      each middleware can inspect/replace the ViewSetResult
     │
     ├─ if lifecycle uses state:  await viewset.save_state()
     │
     ▼
HTTP transport adapter: ViewSetResult.body → JSON response body
                         ViewSetResult.headers/cookies → real Response headers/Set-Cookie
                         ViewSetResult.status_code → real Response status code (e.g. 401)
     │
     ▼
HTTP response
```

Every route builds its context once per request, in the dependency that runs `depends()`; a route
that doesn't declare a `context` parameter is called without it, and the command middleware chain
still receives it. Command middleware, by contrast, is not opt-in per route: if
`settings.viewsets_command_middleware` is non-empty, it wraps every request-facing execution (a
viewset opts *itself* out per-middleware, as `Session` above shows via `@action_configuration`);
with no middleware configured at all, the wrapping is a no-op. "Lifecycle uses state" means
`"singleton"` or `"instance-key"` - see [ViewSet Lifecycle](./lifecycle) for what each mode
actually does and why `"per-request"` skips both hooks, and note `depends()` runs *before*
`load_state()` for `per-request`/`instance-key` lifecycles (no shipped context processor reads
viewset instance state).

| Piece | What it does | Guide |
|---|---|---|
| **Mixins** | Declare which HTTP operations exist (`ListMixin`, `CreateMixin`, ...) and what each one calls (`perform_list`, `perform_create`, ...) | [Python Mixins](./python-mixins) |
| **`route_viewset`** | Registers a viewset class's routes on a FastAPI router; the entry point for every HTTP request | [Routers & Decorators](./routers) |
| **`celery_viewset`** | Optionally moves `perform_*` execution to a Celery worker - transparent to everything else in this list | [Routers & Decorators](./routers) |
| **`CollectionViewSet`** | A ready-made `perform_*` implementation backed by an in-memory collection | [CollectionViewSet](./collection-viewset) |
| **Context processors** | Build a per-request `context` (e.g. the authenticated user) before the endpoint runs | [Context Processors](./context-processors) |
| **Auth backends & `auth_context_processor`** | A pluggable chain of credential schemes contributing `context.user` | [Authentication](./authentication) |
| **Command middleware** | Wrap the actual execution to shape response-level side effects or short-circuit with an error status | [Command Middleware](./command-middleware) |
| **`@action_configuration`** | Per-viewset/per-action configuration for context processors and command middleware to consult (e.g. `Session`'s opt-out) | [Action Configuration](./action-configuration) |
| **Lifecycle & `load_state`/`save_state`** | Controls how the viewset *class* becomes a request-handling *instance*, and whether that instance's own state persists across requests - a separate axis from the pipeline above | [ViewSet Lifecycle](./lifecycle) |

## Context processors and command middleware

- **Context processors** are a flat list (Django *template* `context_processors` style): each one
  independently returns a dict, and the dicts get merged. No processor calls another, none of them
  call the endpoint, none of them see a return value. They gather input facts.
- **Command middleware** is an onion chain (Django *middleware* style): each one wraps `call_next()`
  and can act *before and after* the actual execution, inspecting and replacing what comes back, or
  never calling it at all. It runs the execution inside itself and may change the result or
  whether the execution runs.

A context processor has no "after" phase and cannot stop the chain, so it cannot shape a response
or reject a request. Input gathering in an onion chain would carry unwinding and `call_next` without
using them.

## A note on transports

`Context` and `ViewSetResult` are transport-agnostic types. `route_viewset` is the HTTP
**transport adapter**: it is the only place that knows about a
real `fastapi.Request`/`Response`. The pipeline above - build context, run command middleware,
execute, unwind - doesn't otherwise care whether "the connection" is a single HTTP request/response
or one message on a long-lived WebSocket. The [muxws transport](./muxws) hands each command to the
application as a request, so the application's middleware and exception handlers and this pipeline
run once per command unchanged, and the result's status and headers travel in the reply's leading
headers. `Context.clone_for_command()` makes an
isolated per-command copy of a context (round-tripping through
`serialize_context`/`deserialize_context`), for code that serves several commands from one
context.

## Where to go next

- New to the library? Start with [Python Mixins](./python-mixins) and [Routers & Decorators](./routers).
- Need per-request data like the current user in `perform_*`? See [Context Processors](./context-processors).
- Need pluggable authentication (multiple credential schemes, session-expiry rejection)? See [Authentication](./authentication).
- Need to set a cookie, change the status code, or otherwise shape the response? See [Command Middleware](./command-middleware).
- Need per-viewset/per-action configuration for a processor or middleware (not just one boolean)? See [Action Configuration](./action-configuration).
- Need your viewset instance to remember something between requests? See [ViewSet Lifecycle](./lifecycle).
- Moving execution to a background worker? See the `celery_viewset` section in [Routers & Decorators](./routers).
- Want to see all built-in middleware/processor implementations this library ships (not just the mechanisms behind
  them)? See [Authentication](./authentication), [Authorization](./authorization), and [Rate Limiter](./rate-limiter).
