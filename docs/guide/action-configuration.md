# Action Configuration

A single decorator, `@action_configuration({...})`, for giving context processors and command
middleware per-viewset (or per-action) configuration - instead of a one-off boolean class attribute
per concern (which doesn't scale once more than one processor/middleware needs this).

::: tip Where this fits
[`Session`](./auth#rejecting-unauthenticated-requests-session) (see [Auth](./auth)) is the first
real user of this mechanism - see [Architecture](./architecture#problem-one-boolean-per-concern-doesn-t-scale)
for why a one-off boolean class attribute per concern wouldn't have scaled, and how
`@action_configuration` generalizes that into something any context processor or command
middleware can use, for any concern.
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
rather than once when the configuration was merged - this matters for a hypothetical future WS
transport, where one `Context` (via `clone_for_command()`) could serve several different actions on
one long-lived connection; each read resolves fresh instead of reusing whatever the first action
resolved to.

## Receiving configuration in a context processor

A processor opts in by declaring a parameter literally named `config` (detected by name, the same
convention this library uses for detecting `context` in `route_viewset.py`) - a processor that
doesn't declare it is called exactly as before, unaffected:

```python
async def authz_context_processor(request, viewset, config) -> dict:
    return {"authz": config}   # config is this processor's own resolved entry, or None if unconfigured

settings.viewsets_context_processors = [authz_context_processor]
```

## Receiving configuration in a command middleware

`Middleware` has a `config_from(context)` helper - shorthand for
`context.configuration_for(type(self))`:

```python
from fastapi_viewsets.middleware import Middleware, ViewSetResult

class RateLimiter(Middleware):
    async def __call__(self, request, viewset, context, call_next):
        limit = self.config_from(context) or self.default_limit
        ...
        return await call_next()
```

## Configuring an existing middleware

Key the dict with the `Middleware` **class** to configure whichever instance is already registered
globally:

```python
settings.viewsets_command_middleware = [RateLimiter(limit=100)]

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

## Rejecting a request: just raise

There's no dedicated "reject" hook - a context processor or command middleware that wants to
short-circuit with an error can simply raise `fastapi.HTTPException`. This isn't special machinery:
an exception raised anywhere in the `await` chain that builds context or runs the middleware chain
propagates straight out to FastAPI's own exception handling, exactly like it would from a plain
endpoint function - `save_state()` (see [ViewSet Lifecycle](./lifecycle)) still runs in its
`finally` block either way. Command middleware also has the `ViewSetResult(status_code=...)`
short-circuit shown above for `Session` - either approach works; raise when the rejection logic
lives in a context processor (which has no `ViewSetResult` to return), use `ViewSetResult` when it
lives in command middleware and you also want to shape headers/cookies on the way out.

## Known limitations

- A config-dict identifier meant to name a context processor isn't validated against
  `settings.viewsets_context_processors` (unlike the `Middleware`-class case above) - identifiers
  are opaque and can legitimately be app-defined keys unrelated to any processor/middleware (read
  directly via `context.configuration_for(...)` by your own code), so there's no generic way to
  tell "this was supposed to match something" from "this is a bespoke key."
- `__action_configuration__` at the class level does not merge across inheritance - a subclass that
  redeclares `@action_configuration` fully replaces the base class's version (the same shadowing
  behaviour as any plain class attribute).
