# Command Middleware

An onion chain around the actual `perform_*`/endpoint execution, used for response-level side
effects (e.g. setting a cookie) that can't survive a Celery/Redis round trip on their own.

::: tip Where this fits
Command middleware is one stage in the request pipeline - see [Architecture](./architecture) for
how it relates to `route_viewset`, [Context Processors](./context-processors), and the actual
`perform_*` execution.
:::

---

## Why it exists

An action's return value is JSON-serializable data - but sometimes an endpoint needs to affect the
response itself (most commonly: setting a cookie), not just the response body. This matters
especially when the action is *also* `celery_viewset`-dispatched: the method body may run in a
Celery worker, which has no live `Response` object at all, so the response-level side effect has to
be applied separately, once the result is back in the FastAPI process.

## Configuring middleware

Command middleware is configured on `settings.viewsets_command_middleware`:

```python
from fastapi_viewsets.conf import settings
from fastapi_viewsets.middleware import ViewSetResult

async def session_cookie_middleware(request, viewset, context, call_next):
    result = await call_next()
    if isinstance(result.body, LoginResult):
        data = result.body.model_dump()
        session_key = data.pop("session_key", None)
        if session_key is not None:
            result.cookies["sessionid"] = session_key
        result.body = data           # becomes the actual JSON body
    return result

session_cookie_middleware.modifies_response_shape = True  # strips a field - see below

settings.viewsets_command_middleware = [session_cookie_middleware]
```

Each middleware is `async def middleware(request, viewset, context, call_next) -> ViewSetResult`.
`call_next()` (no arguments - `context` and the viewset instance are the only mutable structures in
the pipeline, so a middleware that wants to influence what happens next mutates `context` in place
rather than threading a new one through) invokes the next middleware, or the actual execution at
the innermost layer, and returns the `ViewSetResult` it produced - which this middleware can inspect
and mutate before returning it back up the chain. Middleware run in list order on the way in, and
unwind in reverse order on the way out (a standard onion/middleware chain).

## Class-based middleware: `Middleware`

A plain function is enough for a one-off closure, but reusable, ready-to-use middleware (shipped as
library code rather than written inline in an app) reads better as a class. `Middleware` is a tiny
ABC for exactly that - implement `__call__` with the same signature a function would have:

```python
from fastapi_viewsets.middleware import Middleware, ViewSetResult

class SessionCookieMiddleware(Middleware):
    modifies_response_shape = True   # strips a field - see below

    async def __call__(self, request, viewset, context, call_next):
        result = await call_next()
        ...
        return result

settings.viewsets_command_middleware = [SessionCookieMiddleware()]
```

`run_command_chain` doesn't need to know or care whether an entry in
`settings.viewsets_command_middleware` is a bare function or a `Middleware` instance - both are just
called as `middleware(request, viewset, context, call_next)`. See
[Authentication](./authentication#rejecting-unauthenticated-requests-session) for `Session`, a
`Middleware` shipped by this library - though, as of the next section, most of its actual logic
doesn't live in `__call__` at all anymore.

## Early rejection: `Middleware.depends()`

A pure "check and maybe reject" middleware - no "after" phase, never inspects/reshapes what
`call_next()` returns - doesn't actually need the onion chain's timing at all: it only ever needs
to run *before* `perform_*`. `Middleware` has an optional second method, `depends()`, for exactly
this case - bridged by `route_viewset` onto FastAPI's own native `Depends()` mechanism, so it runs
before FastAPI even parses the request body:

```python
from fastapi import HTTPException
from fastapi_viewsets.middleware import Middleware

class RequireAdmin(Middleware):
    async def depends(self, request, cls, context) -> None:
        user = await context.user
        if user is None or not user.get("is_admin"):
            raise HTTPException(status_code=403, detail="Admins only")

    async def __call__(self, request, viewset, context, call_next):
        return await call_next()   # depends() already decided everything - nothing left to do here
```

This gets: rejection *before* FastAPI parses the body (a `403`/`401`/`429` can now precede a `422`
for a malformed body on the same request - the conventional, correct ordering for real APIs),
native OpenAPI/Swagger discoverability (see `settings.viewsets_security_scheme` below),
`app.dependency_overrides` testability (impossible with the pure onion-chain mechanism, since it's
never part of FastAPI's own dependency graph), and - since `depends()` is entirely optional -
existing plain-function middleware and `Middleware` subclasses that don't define it are completely
unaffected, running only in the onion chain exactly as before.

`context` is real and usable inside `depends()` (built as early as possible - see
[Context Processors](./context-processors) - and shared/cached for the rest of the request, so
context processors never run twice), but `viewset` is not: `cls` (the viewset *class*, not an
instance) is passed instead, since for `per-request`/`instance-key` lifecycles no instance exists
yet this early (uniform across all three lifecycle modes, rather than sometimes a real instance and
sometimes not).

**Put only reject-capable logic in `depends()`.** Work that always happens regardless of any
decision (e.g. enriching `context` with no reject path) has no early-timing benefit and should stay
in `__call__`, exactly as it would without `depends()` at all - see
[Authorization](./authorization), which splits across both methods for precisely this reason: the
callable-config check (might reject) lives in `depends()`, while exposing `context.authorization`
for `perform_*` to read (only when the config isn't itself the check) stays in `__call__`.

### OpenAPI/Swagger discoverability: `viewsets_security_scheme`

`depends()` alone gets you correct timing, but no lock icon in Swagger UI - that requires a real
[`fastapi.security`](https://fastapi.tiangolo.com/tutorial/security/) scheme somewhere in the
dependency tree. Set one globally and `route_viewset` attaches it as an extra sub-dependency
alongside the `depends()` bridge on every route, purely for OpenAPI's benefit (this library never
reads its resolved value itself):

```python
from fastapi.security import APIKeyHeader
from fastapi_viewsets.conf import settings

settings.viewsets_security_scheme = APIKeyHeader(name="X-Session-Token", auto_error=False)
```

Must be set **before** any viewset class is decorated with `route_viewset` (the same existing
constraint `any_modifies_response_shape(settings.viewsets_command_middleware)` already has in this
codebase - see below) - typically at app startup, before importing viewset modules.

## Per-viewset/per-action configuration

A `Middleware` reads its own [`@action_configuration`](./action-configuration) value via
`self.config_from(context)` (shorthand for `context.configuration_for(type(self))`) - works the
same whether read from `depends()` or `__call__`:

```python
class RateLimiter(Middleware):
    async def depends(self, request, cls, context):
        config = self.config_from(context)
        limit = self.default_limit if config is None else config
        ...
```

This is how one globally-registered middleware instance can behave differently per viewset or
per action - see [Action Configuration](./action-configuration) for the full merge rules
(global default → class → method) and for injecting a brand-new middleware just for one
viewset/method without registering it globally at all. [`RateLimiter`](./rate-limiter) is exactly
this middleware, for real - one of a few
[built-in middleware/processor implementations](./authentication) this library ships.

## `ViewSetResult`

```python
@dataclass
class ViewSetResult(SerializableObject, Generic[T]):
    body: T
    headers: dict[str, Any] = field(default_factory=dict)
    cookies: dict[str, Any] = field(default_factory=dict)
    status_code: int | None = None
```

Transport-agnostic: `body` is the actual domain value (whatever `perform_*` returned), `headers`/
`cookies` are side-channel metadata - not necessarily real HTTP headers/cookies, since the same
mechanism is meant to work over other transports too (see [Architecture](./architecture#a-note-on-transports)).
`route_viewset` (the HTTP transport adapter) applies `headers` onto the real `Response`'s headers
and `cookies` via `response.set_cookie(...)` once the chain finishes - regardless of whether the
action ran in-process or was `celery_viewset`-dispatched to a worker.

`status_code`, when set, lets a middleware short-circuit the *onion chain* with a non-200 result
without ever calling `call_next()` - no special mechanism is needed for this, since a middleware
simply returning without calling `call_next()` already means nothing further down the chain runs.
`route_viewset` applies it onto the real `Response`'s status code; left `None` (the default), the
response's status code is unaffected. For a middleware whose *entire* job is reject-or-allow with
no "after" phase (e.g. `Session`'s `401`), prefer raising `HTTPException` from `depends()` instead
(see above) - it runs earlier and gets OpenAPI/testability benefits `status_code` alone doesn't.

With no middleware configured (the default), behaviour is unchanged: the endpoint's return value
becomes the response body as-is, no headers/cookies are touched.

### Returning `ViewSetResult` directly from an endpoint

A registered route endpoint returns either its declared model directly - the simple, REST-like
case, where FastAPI's own response handling is all there is to it - or a `ViewSetResult`, for full
control over the result: `status_code`/`headers`/`cookies` alongside (or instead of) the body,
without writing a command middleware for it:

```python
from fastapi_viewsets.middleware import ViewSetResult

class InviteViewSet:
    __router = APIRouter()

    @__router.post("accept")
    async def accept(self, context: Context) -> ViewSetResult[None]:
        await self.mark_accepted(context)
        return ViewSetResult(body=None, status_code=302, headers={"Location": "/welcome"})
```

The transport adapter doesn't care where a `ViewSetResult` came from: `lifecycle_runner` recognizes
one returned directly and passes it through unchanged (instead of wrapping it in another
`ViewSetResult`, which would bury `status_code`/`headers`/`cookies` one level too deep to ever be
applied), and any globally-configured command middleware still runs around it exactly as it would
around a plain-body endpoint. The example above is a redirect: a browser or `restViewSet` client
follows a `3xx` status with a `Location` header regardless of what the body is (see
[Handling a failed call](./vue-mixins#handling-a-failed-call)), so `body=None` is enough - there is
no JSON payload for a client to read here.

Declare the return type as `ViewSetResult[X]` rather than bare `X` - `route_viewset`/`build_schema`
unwrap it to `X` for the OpenAPI `response_model` and FastAPI's own response validation, so
`-> ViewSetResult[X]` documents/validates exactly like a plain `-> X` would. `X` is `None` above
because this particular endpoint never sends a body; an endpoint whose `ViewSetResult` sometimes
carries real data declares whatever that data's type actually is (`ViewSetResult[Item]`, say).

**Only the registered route endpoint can do this - a `perform_*` hook cannot.**
`perform_list`/`perform_create`/etc. are never registered as routes themselves; the mixin's own
route method (`list_items`, `create`, ...) calls them and does further work with the result
(pagination, filtering, shaping) that expects plain records, not a `ViewSetResult`. A custom
`__router` method has no such pipeline around it - it *is* the registered endpoint - which is why
`ViewSetResult` only ever applies there.

`ViewSetResult` is itself a `SerializableObject` (see [Context Processors](./context-processors)),
the same mechanism `LazyObject`/`Context` values already use to survive the Celery/Redis boundary -
a `celery_viewset`-dispatched action's worker-side return value is tagged and reconstructed exactly
like any other `SerializableObject`, so this works whether the action runs in-process or in a
worker.

## OpenAPI response schema: `modifies_response_shape`

Most middleware only attaches `headers`/`cookies` or a `status_code` - it never touches `result.body`
at all, so the endpoint's declared return type is still exactly what gets served. For that common
case, `route_viewset` keeps using it as the route's `response_model`: the OpenAPI docs stay typed,
and FastAPI still validates/serializes the response against it, same as with no middleware
configured at all.

A middleware whose `__call__` reassigns `result.body` to something that no longer matches the
original return annotation (like `session_cookie_middleware`/`SessionCookieMiddleware` above,
stripping `session_key`) must declare `modifies_response_shape = True` - as a class attribute on a
`Middleware` subclass, or set on the function object for a plain-function middleware
(`my_middleware.modifies_response_shape = True`). This tells `route_viewset` the declared return
type can no longer be trusted, so it registers the route with `response_model=None` instead: FastAPI
serves whatever `result.body` actually is, without re-validating/coercing it back towards the
original model (which would silently undo the reshaping - e.g. reintroducing a stripped field with
its default value) - at the cost of an untyped response schema in the OpenAPI docs for that route.

Default is `False` on both `Middleware` and plain functions - a middleware that doesn't set it is
assumed to leave `result.body` matching the endpoint's declared model exactly. This includes adding
fields the model doesn't declare: with `modifies_response_shape` left `False`, FastAPI's response
validation silently drops any such field from what's actually served, since it still validates
against the original model. A middleware that adds fields, not just one that strips/renames them,
needs `modifies_response_shape = True` too - and so does one that short-circuits the chain (returns
without calling `call_next()`, see `status_code` above) with a `body` of its own that doesn't match
the declared model, e.g. an error payload. Prefer raising `HTTPException` from `depends()` instead
for that case (see [Early rejection](#early-rejection-middleware-depends) above) - it never goes
through response_model validation at all, so this doesn't come up.

## Only runs in the FastAPI process

Like the context-processor's `Request`, command middleware only ever runs in the outer,
HTTP-facing `route_viewset` call - never inside `celery_viewset_server`'s inner, worker-only call
(there's no live `Response` there to shape, and no `Depends()` concept either). A
`celery_viewset`-dispatched action's core logic runs in the worker; both `depends()` and the onion
chain run once (before dispatch and once the result is back, respectively) in the FastAPI process -
`depends()` in particular means an action never even reaches the worker if it was going to be
rejected anyway.

## Known limitations

- No WebSocket transport exists in this library yet. `ViewSetResult` (and `Context`, from
  [Context Processors](./context-processors)) are designed to be transport-agnostic - a future WS
  adapter would run the command middleware chain once per message on a long-lived connection, using
  `Context.clone_for_command()` to give each command an isolated copy - but only the HTTP adapter is
  actually implemented today.
- `depends()` (see above) runs before `load_state()` for `per-request`/`instance-key` lifecycles
  (previously always after, back when everything ran in the onion chain) - accepted since no
  shipped context processor reads viewset instance state.
- `load_state`/`save_state` (see [ViewSet Lifecycle](./lifecycle)) remain a separate mechanism -
  command middleware doesn't replace them.
