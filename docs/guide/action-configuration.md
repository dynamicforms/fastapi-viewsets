# Action Configuration

A single decorator, `@action_configuration({...})`, for giving context processors and command
middleware per-viewset (or per-action) configuration - instead of a one-off boolean class attribute
per concern (which doesn't scale once more than one processor/middleware needs this).

::: tip Where this fits
[`Session`](./authentication#rejecting-unauthenticated-requests-session) (see
[Authentication](./authentication)) reads its opt-out through this mechanism - see
[Architecture](./architecture#requirement-per-viewset-configuration) for why per-viewset settings
use one mechanism for every context processor and command middleware rather than a class
attribute per concern.
:::

---

## The decorator

Works identically whether applied to a viewset class or to an individual method (typically a
`perform_*` method, where the actual logic lives):

```python
from fastapi_viewsets.action_configuration import action_configuration

@action_configuration({SomeIdentifier: "some-value", AnotherIdentifier: 5})
class ItemViewSet(...): ...

class ItemViewSet(...):
    @action_configuration({SomeIdentifier: "some-value"})
    async def perform_update(self, context, ...): ...
```

The dict's **keys are identifiers** - a context processor callable already registered in
`settings.viewsets_context_processors`, a `Middleware` subclass already registered in
`settings.viewsets_command_middleware` (see [Configuring an existing middleware](#configuring-an-existing-middleware)
below), or a fresh `Middleware` instance to inject just for this call (see
[Injecting a new middleware](#injecting-a-new-middleware-just-for-one-viewset-method)). The **values
are opaque** - the framework never interprets them, only delivers them to whichever
processor/middleware reads them.

## Merge order: default → class → method

```python
from fastapi_viewsets.conf import settings

settings.default_action_configuration = {SomeIdentifier: "fallback-value"}
```

For a given call, configuration is merged **per identifier** - `settings.default_action_configuration`
(lowest priority) → the viewset class's `@action_configuration` → the specific method's
`@action_configuration` (highest priority). A method-level entry replaces the class-level entry for
*that identifier only* - other identifiers the method doesn't mention still fall through from the
class or the global default:

```python
@action_configuration({Session: True, RateLimiter: 5})
class ItemViewSet(...):
    @action_configuration({Session: False})   # only overrides Session for this one action
    async def perform_create(self, context, data): ...
    # every other action on this viewset: Session required, RateLimiter still 5
```

Method-level lookup checks both the actual action name (for hand-written `__router` endpoints,
which have no separate `perform_*` counterpart) and the `perform_*` method it maps to (`update`/
`partial_update` → `perform_update`, `list_items` → `perform_list`, etc.) - so decorating
`perform_update` (what you actually write) works exactly as decorating `update` (the framework's
own `@final` method) would.

## Varying a value per action: `ByAction`

A single class-level entry can mean different things depending on which action is currently
running:

```python
from fastapi_viewsets.context import ByAction

@action_configuration({authz_context_processor: ByAction(list_items="read", update="write", default="read")})
class ItemViewSet(...): ...
```

`ByAction` is resolved at **read time** (against whichever action a `Context` currently represents)
rather than once when the configuration was merged: where one `Context` (via
`clone_for_command()`) serves several actions, each read resolves for the action it serves.

## Receiving configuration in a context processor

A processor opts in by declaring a parameter literally named `config` (detected by name, the same
convention this library uses for detecting `context` in `route_viewset.py`); a processor that
doesn't declare it is called without it:

```python
async def authz_context_processor(request, viewset, config) -> dict:
    return {"authz": config}   # config is this processor's own resolved entry, or None if unconfigured

settings.viewsets_context_processors = [authz_context_processor]
```

## Receiving configuration in a command middleware

`Middleware` has a `config_from(context)` helper - shorthand for
`context.configuration_for(type(self))`:

```python
from fastapi_viewsets.exceptions import RateLimitedError
from fastapi_viewsets.middleware import Middleware

class RateLimiter(Middleware):
    async def depends(self, request, cls, context):
        config = self.config_from(context)
        limit = self.default_limit if config is None else config
        if not await self._increment(self.key_func(request, cls, context), limit):
            raise RateLimitedError()
```

`config_from(context)` reads the same in `depends()` and in `__call__`. `RateLimiter` is a
ready-to-use middleware shipped with this library, and decides in `depends()`, before the request
body is parsed - see [Rate Limiter](./rate-limiter) for the full implementation.

## Configuring an existing middleware

Key the dict with the `Middleware` **class** to configure whichever instance is already registered
globally:

```python
settings.viewsets_command_middleware = [RateLimiter(default_limit=100)]

@action_configuration({RateLimiter: 10})   # this viewset gets a stricter limit
class ExpensiveViewSet(...): ...
```

A class key with no matching instance anywhere in `settings.viewsets_command_middleware` raises
`ValueError` at request time, fail-fast - almost certainly a typo or a forgotten global
registration, not an intentional injection (use an instance for that - see below).

## Injecting a new middleware, just for one viewset/method

Key the dict with a `Middleware` **instance** instead of a class - it's appended to the end of the
chain (after the global list), only for calls that resolve this configuration:

```python
@action_configuration({AuditLogMiddleware(): None})
class SensitiveViewSet(...): ...
```

The instance is constructed once (at class-body/decoration time, like any decorator argument) and
shared across every request to that viewset/method - same as a globally-registered middleware
singleton would be.

## Time limits of a deferred action

`TimeLimits` (in `fastapi_viewsets.action_configuration`) is a key the library itself reads: the
time limits, in seconds, of an execution of a [`celery_viewset`](./routers#time-limits) action.

```python
from fastapi_viewsets.action_configuration import action_configuration, TimeLimits

@action_configuration({TimeLimits: TimeLimits(soft=30, hard=60)})
class ReportViewSet(...):
    @action_configuration({TimeLimits: TimeLimits(soft=300, hard=330)})
    async def perform_create(self, context, data): ...

settings.default_action_configuration = {TimeLimits: TimeLimits(soft=120)}
```

| Field | Default | When it passes |
|-------|---------|----------------|
| `soft` | `None` | `Middleware.execute()` sees `ActionInterruptedError` from `call_next()`, `finally` blocks and `save_state()` run, and the caller receives `ActionInterruptedError` (504, reason `time_limit`) |
| `hard` | `None` | the worker ends the process executing the action; the caller receives the same error and `Middleware.on_transition()` receives `interrupted` |

`None` leaves that limit to the worker's own configuration. The entry merges like any other: a
method-level `TimeLimits` replaces the class-level one whole, and `ByAction` varies it per action.
An action executed in the FastAPI process has no time limit.

## Rejecting a request: just raise

There's no dedicated "reject" hook - a context processor or command middleware that wants to
short-circuit with an error raises `fastapi.HTTPException`. An exception raised anywhere in the
`await` chain that builds context or runs the middleware chain propagates to FastAPI's own
exception handling, as it would from a plain endpoint function - `save_state()` (see
[ViewSet Lifecycle](./lifecycle)) still runs in its `finally` block either way. A command
middleware's `__call__` can also return `ViewSetResult(status_code=...)` without calling
`call_next()`, which rejects the call and shapes its headers and cookies on the way out; a context
processor or a `depends()` has no result to return and raises.

## Known limitations

- A config-dict identifier meant to name a context processor isn't validated against
  `settings.viewsets_context_processors` (unlike the `Middleware`-class case above) - identifiers
  are opaque and can legitimately be app-defined keys unrelated to any processor/middleware (read
  directly via `context.configuration_for(...)` by your own code), so there's no generic way to
  tell "this was supposed to match something" from "this is a bespoke key."
- `__action_configuration__` at the class level does not merge across inheritance - a subclass that
  redeclares `@action_configuration` fully replaces the base class's version (the same shadowing
  behaviour as any plain class attribute).
