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
    async def __call__(self, request, viewset, context, call_next):
        result = await call_next()
        ...
        return result

settings.viewsets_command_middleware = [SessionCookieMiddleware()]
```

`run_command_chain` doesn't need to know or care whether an entry in
`settings.viewsets_command_middleware` is a bare function or a `Middleware` instance - both are just
called as `middleware(request, viewset, context, call_next)`. See [Auth](./auth#rejecting-unauthenticated-requests-session)
for `Session`, a `Middleware` shipped by this library.

## Per-viewset/per-action configuration

A `Middleware` reads its own [`@action_configuration`](./action-configuration) value via
`self.config_from(context)` (shorthand for `context.configuration_for(type(self))`):

```python
class RateLimiter(Middleware):
    async def __call__(self, request, viewset, context, call_next):
        limit = self.config_from(context) or self.default_limit
        ...
        return await call_next()
```

This is how one globally-registered middleware instance can behave differently per viewset or
per action - see [Action Configuration](./action-configuration) for the full merge rules
(global default → class → method) and for injecting a brand-new middleware just for one
viewset/method without registering it globally at all.

## `ViewSetResult`

```python
@dataclass
class ViewSetResult:
    body: Any
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

`status_code`, when set, lets a middleware short-circuit the chain with a non-200 result (e.g. `401`
for an expired session, see [Auth](./auth)) without ever calling `call_next()` - no special
mechanism is needed for this, since a middleware simply returning without calling `call_next()`
already means nothing further down the chain runs. `route_viewset` applies it onto the real
`Response`'s status code; left `None` (the default), the response's status code is unaffected.

With no middleware configured (the default), behaviour is unchanged: the endpoint's return value
becomes the response body as-is, no headers/cookies are touched.

## Only runs in the FastAPI process

Like the context-processor's `Request`, command middleware only ever runs in the outer,
HTTP-facing `route_viewset` call - never inside `celery_viewset_server`'s inner, worker-only call
(there's no live `Response` there to shape). A `celery_viewset`-dispatched action's core logic runs
in the worker; the middleware chain runs once the result is back in the FastAPI process.

## Known limitations

- No WebSocket transport exists in this library yet. `ViewSetResult` (and `Context`, from
  [Context Processors](./context-processors)) are designed to be transport-agnostic - a future WS
  adapter would run the command middleware chain once per message on a long-lived connection, using
  `Context.clone_for_command()` to give each command an isolated copy - but only the HTTP adapter is
  actually implemented today.
- `load_state`/`save_state` (see [ViewSet Lifecycle](./lifecycle)) remain a separate mechanism -
  command middleware doesn't replace them.
