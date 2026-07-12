# Architecture

This page builds up the full request pipeline one real problem at a time, using a single running
example (an `Item` viewset that needs authentication). Every guide page it links to
([Context Processors](./context-processors), [Command Middleware](./command-middleware),
[Action Configuration](./action-configuration), [Authentication](./authentication),
[Authorization](./authorization), [ViewSet Lifecycle](./lifecycle),
[Routers & Decorators](./routers)) is reference material - exhaustive, structured, meant to be
looked up once you already know what you're looking for. This page is the opposite: read it top to
bottom once, and you'll know *why* each of those pieces exists before you ever need to look one up.

---

## Start minimal

```python
database: dict[int, Item] = {1: Item(id=1, name="Widget")}

@route_viewset(router, base_path="/items")
class ItemViewSet(CollectionViewSet[int, Item]):
    def __init__(self):
        super().__init__(container=database, pk_field="id")
```

That's a complete, working `GET /items` (and `POST`, `PUT`, `DELETE`, ...) endpoint. No context, no
middleware, no lifecycle concerns - `route_viewset` registers the routes, `CollectionViewSet`
implements every `perform_*` against the in-memory `database` dict. For a lot of viewsets, this is
the whole story. Now let's add requirements one at a time and see what each one forces us to add.

## Problem: "who's calling?"

Say `ItemViewSet` needs to know who's making the request - to filter results, to stamp an owner
field, whatever. `perform_list(self)` has no way to see the request at all: `route_viewset` calls
it with no arguments, deliberately, because most viewsets don't need one.

The fix is a parameter, `context`, that carries per-request facts:

```python
class ItemViewSet(CollectionViewSet[int, Item]):
    async def perform_list(self, context: Context) -> list[Item]:
        user = await context.user
        return [i for i in database.values() if i.owner == user]
```

Something has to populate `context.user` *before* `perform_list` runs, from the live `Request` -
a **context processor**:

```python
async def whoami_processor(request: Request, viewset) -> dict:
    return {"user": request.headers.get("x-user")}

settings.viewsets_context_processors = [whoami_processor]
```

`route_viewset` sees that `perform_list` declares a `context` parameter, builds one by running
every configured processor and merging their dicts, and passes it in. `context.user` is *always*
awaitable - even here, where the value is already a plain string sitting in a dict - because the
next section is about to make that matter for real. → full depth in
[Context Processors](./context-processors).

## Problem: "real auth has more than one way to prove who you are"

A single `x-user` header is a toy. A real app might accept an `X-Session-Token` against a fixed,
ad-hoc list of users in one deployment, and a real Django session in another - or both at once,
tried in order until one of them recognizes the request. `whoami_processor` above can't grow into
that cleanly; it would turn into a pile of if/elif branches for every credential scheme the app
ever wants to support.

Instead, the credential schemes become **backends** - each one a `try_handle(request)` that either
says "not mine, try the next one" (`None`) or claims the request and returns something that
resolves to the user later:

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

Notice `try_handle` returns a `LazyObject`, not the user itself - resolving a real Django session
means a DB/cache lookup, which is exactly the kind of work you don't want to pay for on every
request whether or not `perform_*` actually reads `context.user`. It's also exactly the kind of
value that can't just be `deepcopy`'d across the Celery/Redis boundary if the action is
`celery_viewset`-dispatched: the *recipe* (a session key) has to travel, not a live database
object, so the worker can resolve it again on its own side. `LazyObject` (a `SerializableObject`
subclass with lazy, memoized resolution) is what makes `context.user` uniformly awaitable *and*
correctly (de)serializable regardless of which backend produced it. → full depth in
[Authentication](./authentication) and the `LazyObject`/`SerializableObject` section of
[Context Processors](./context-processors).

## Problem: "a context processor can't say no"

Now suppose an expired or missing session should make the request fail with `401`, before
`perform_list` ever runs. It's tempting to reach for the same mechanism again - can't
`auth_context_processor` just... reject the request?

It can't, and not as an oversight: context processors are a **flat list** (Django *template*
`context_processors` style). Each one independently returns a dict; the dicts get merged; none of
them call the endpoint, none of them see what it returns, none of them can stop the others from
running. That flatness is exactly what makes them simple and safe to add to - but it also means
there is no hook anywhere in that list for "stop here and produce a `401` instead." Gathering facts
and deciding what to do about them are different-shaped problems.

**Command middleware** is the other shape - an onion chain wrapping the actual execution, each
layer calling `call_next()` for the next one. A middleware is just anything callable with the right
signature - a plain function, or a class implementing `Middleware.__call__` (useful when, as here,
the middleware is reusable library code rather than a one-off closure):

```python
class Session(Middleware):
    async def __call__(self, request, viewset, context, call_next):
        config = self.config_from(context)
        required = True if config is None else bool(config)
        if not required:
            return await call_next()
        user = await context.user
        if user is None:
            return ViewSetResult(body={"detail": "Session expired or invalid"}, status_code=401)
        return await call_next()

settings.viewsets_command_middleware = [Session()]
```

Because it's onion-shaped, "reject" needs no special mechanism at all: a middleware that returns
without calling `call_next()` simply means nothing further down the chain - not `perform_list`, not
any middleware configured after it - ever runs. `ViewSetResult.status_code` carries the `401` all
the way out to the real HTTP response. (401, not 403: the credential itself no longer establishes
who's calling - missing, garbage, or expired - which is "unauthorized," not "authorized but
forbidden to do this specific thing.") A viewset that must stay reachable without a session (a
login endpoint, say) opts out with `@action_configuration({Session: False})` - `self.config_from(context)`
is exactly how `Session` reads that. → full depth in [Command Middleware](./command-middleware) and
[Authentication](./authentication).

This is also the clearest illustration of why the two mechanisms stay separate: the context
processor only ever *gathered* the fact ("user is `None`"); it's the command middleware that
*decided* what to do about that fact and reshaped the response. Neither could have done the other's
job.

## Problem: "one boolean per concern doesn't scale"

`Session`'s opt-out could have been solved with a single bespoke class attribute -
`requires_auth = False`, read via `getattr(viewset, "requires_auth", True)` - and that's tempting
for exactly one concern. But suppose a second requirement shows up: some viewsets also need a
per-viewset rate limit, and the limit itself varies (5/minute here, 100/minute there). Following
the same pattern means another bespoke attribute (`rate_limit = 5`), read by another middleware via
another bespoke `getattr(...)` - and a third concern repeats the dance again. Nothing here is
reusable; every processor/middleware that ever wants per-viewset behaviour would reinvent its own
attribute-with-a-default convention.

`@action_configuration({...})` is the general mechanism instead of one-boolean-per-concern: a
decorator (usable on a viewset class or an individual method) carrying a dict keyed by whichever
context processor or `Middleware` cares - the value is opaque, interpreted only by that
identifier's own code. `Session.config_from(context)`, shown above, is shorthand for
`context.configuration_for(type(self))` - this is what actually resolves the opt-out:

```python
from fastapi_viewsets.action_configuration import action_configuration

@action_configuration({Session: False, RateLimiter: 5})
class PublicButThrottledViewSet(...): ...
```

Configuration merges per identifier across three layers - `settings.default_action_configuration`
(global fallback) → the viewset class's `@action_configuration` → an individual method's
`@action_configuration` (highest priority, overriding just its own identifiers - other identifiers
the method doesn't mention still fall through from the class). A value can even vary by action
(`ByAction(list_items="read", update="write")`), resolved fresh each time against whichever action
is actually running. → full depth in [Action Configuration](./action-configuration), and see
[Rate Limiter](./rate-limiter) and [Authorization](./authorization) for `RateLimiter` and
`Authorization`, two real middleware shipped with this library that build on it.

## Problem: "my viewset needs to remember something that isn't about who's calling"

A different axis, easy to conflate with the above: what if the *viewset instance itself* needs to
remember something across requests - a cache, a counter, an open connection - independent of which
user is calling? That's not context (a per-request fact about the caller) and not command
middleware (a response-shaping step); it's the viewset's own state, and it's controlled by the
`lifecycle` parameter plus two optional hooks:

```python
@route_viewset(router, base_path="/session", lifecycle="instance-key")
class SessionViewSet(ListMixin[str]):
    async def load_state(self):
        self.items = await redis.lrange("session:items", 0, -1)

    async def save_state(self):
        await redis.rpush("session:items", *self.items)
```

`load_state()` runs before context is even built; `save_state()` runs after the command middleware
chain unwinds, in a `finally` block, so it runs even if something above raised. → full depth in
[ViewSet Lifecycle](./lifecycle).

## Problem: "this needs to run in the background"

Last requirement: `perform_list` turns out to be slow enough that it shouldn't block the request -
it should run on a Celery worker instead. `celery_viewset` does this transparently: the same
`ItemViewSet` class, decorated once more, dispatches its `perform_*` methods to a worker and awaits
the result over Redis instead of running them in-process.

The one thing that has to survive that trip is `context` - the worker has no live `Request` to
build one from. That's exactly what `serialize_context`/`deserialize_context` are for: plain values
cross as-is, a `SerializableObject` crosses as a tagged JSON payload, and a `LazyObject` (like
`context.user`, from the auth backends above) crosses as its unresolved *recipe* - the worker reconstructs
it and can still resolve it lazily, on its own side, only if the worker-side `perform_list`
actually awaits it. None of the auth code from earlier sections has to know or care that this
happens. → full depth in [Routers & Decorators](./routers).

## The request pipeline

Pulling every piece from the sections above into one picture, for a single HTTP request, in order:

```
HTTP request
     │
     ▼
route_viewset-registered endpoint (lifecycle: singleton / per-request / instance-key)
     │
     ├─ if lifecycle uses state:  await viewset.load_state()
     │
     ├─ if the endpoint declares `context`:
     │      build Context by running settings.viewsets_context_processors
     │      (needs the live Request - see Context Processors / Auth)
     │
     ▼
Command middleware chain (settings.viewsets_command_middleware) — before phase, in list order
     │      (e.g. Session can short-circuit here with a 401)
     ▼
perform_* execution
     │      (optionally delegated: celery_viewset ships `context` to a worker,
     │       awaits the result over Redis, and returns transparently)
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

A route that doesn't declare a `context` parameter skips the context-building step entirely (no
`Request` is even injected) - a genuinely zero-cost opt-out, not just "context ends up empty".
Command middleware, by contrast, is not opt-in per route: if `settings.viewsets_command_middleware`
is non-empty, it wraps every request-facing execution (a viewset opts *itself* out per-middleware,
as `Session` above shows via `@action_configuration`); with no middleware configured at all, the
wrapping is a no-op. "Lifecycle uses state" means `"singleton"` or `"instance-key"` -
see [ViewSet Lifecycle](./lifecycle) for what each mode actually does and why `"per-request"` skips
both hooks.

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

## Why two separate mechanisms, not one

You just saw why, concretely, two sections up: the context processor could gather "user is
`None`," but had no shape for stopping the request or touching the response; command middleware
could reject with a `401` and did, precisely because it wraps execution instead of just preceding
it. Stated more generally:

- **Context processors** are a flat list (Django *template* `context_processors` style): each one
  independently returns a dict, and the dicts get merged. No processor calls another, none of them
  call the endpoint, none of them see a return value. Right shape for "gather some input facts."
- **Command middleware** is an onion chain (Django *middleware* style): each one wraps `call_next()`
  and can act *before and after* the actual execution, inspecting and replacing what comes back, or
  never calling it at all. Right shape for "the execution needs to run **inside** something, and its
  result (or whether it runs at all) may need to change."

Trying to force response-shaping or short-circuiting into the flat, request-only shape of context
processors doesn't work: there's no "after" phase, and no "don't call the next one" phase, to hook
into. Trying to force simple input gathering into an onion chain works, but adds ordering
complexity (unwinding phases, `call_next`) for no benefit when nothing needs to happen after the
endpoint runs, or ever needs to reject anything. Two shapes, two mechanisms.

## A note on transports

Both `Context` and `ViewSetResult` are deliberately transport-agnostic types, not HTTP-specific
ones. `route_viewset` is the HTTP **transport adapter**: it's the only place that knows about a
real `fastapi.Request`/`Response`. The pipeline above - build context, run command middleware,
execute, unwind - doesn't otherwise care whether "the connection" is a single HTTP request/response
or one message on a long-lived WebSocket. No WS transport exists in this library yet, but
`Context.clone_for_command()` (an isolated per-command copy, built by round-tripping through
`serialize_context`/`deserialize_context`) exists specifically so a future WS adapter can run this
same pipeline once per message on one connection, without retrofitting `Context`/`ViewSetResult`
themselves.

## Where to go next

- New to the library? Start with [Python Mixins](./python-mixins) and [Routers & Decorators](./routers).
- Need per-request data like the current user in `perform_*`? See [Context Processors](./context-processors).
- Need pluggable authentication (multiple credential schemes, session-expiry rejection)? See [Authentication](./authentication).
- Need to set a cookie, change the status code, or otherwise shape the response? See [Command Middleware](./command-middleware).
- Need per-viewset/per-action configuration for a processor or middleware (not just one boolean)? See [Action Configuration](./action-configuration).
- Need your viewset instance to remember something between requests? See [ViewSet Lifecycle](./lifecycle).
- Moving execution to a background worker? See the `celery_viewset` section in [Routers & Decorators](./routers).
- Want to see all built-in middleware/processor implementations this library ships (not just the mechanisms behind them)? See [Authentication](./authentication), [Authorization](./authorization), and [Rate Limiter](./rate-limiter).
