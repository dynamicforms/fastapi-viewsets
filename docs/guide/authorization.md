# Authorization

`fastapi_viewsets.middleware.auth.authorization.Authorization` answers "is this caller allowed to
do *this*" - a command middleware built on the general [Action Configuration](./action-configuration)
mechanism, one of a few [built-in middleware/processor implementations](./rate-limiter) this
library ships.

::: tip Loosely coupled with Authentication, not layered on top of it
Nothing in `Authorization`'s code references `context.user`, `Session`, or anything else from
[Authentication](./authentication) - it's a fully independent middleware. An authorization check
just needs *some anchor* to decide against (in the examples below, `context.user`, because that's
what this library's built-in authentication happens to produce) - which authentication mechanism
provided that anchor, or whether one even did, is none of `Authorization`'s concern. Swap in a
completely different authentication scheme (a different "dimension" of identity entirely) and
`Authorization` doesn't change at all.
:::

::: tip Where this fits
See [Architecture](./architecture#problem-one-boolean-per-concern-doesn-t-scale) for why this and
[Session](./authentication#rejecting-unauthenticated-requests-session)'s opt-out both needed a
general per-viewset configuration mechanism rather than one-off class attributes.
:::

---

## Two ways to configure it

Configured via [`@action_configuration`](./action-configuration), keyed by the `Authorization`
class. The value plays one of two roles depending on its shape:

- **Callable** - evaluated as `check(request, cls, context)` (sync or async) in `depends()` -
  bridged onto FastAPI's own native `Depends()` by `route_viewset` (see
  [Command Middleware](./command-middleware#early-rejection-middleware-depends)) - a falsy result
  rejects the request with `403` before `perform_*` (or even FastAPI's own body parsing) ever runs.
  `cls` is the viewset *class*, not an instance (no instance exists yet this early - see
  [Command Middleware](./command-middleware#early-rejection-middleware-depends)); `context` is real
  and usable, so e.g. `is_admin(context)` still works exactly as shown below. Use this for checks
  that don't need a specific record (a role check, say).
- **Anything else** (or unconfigured, `None`) - not enforced by the middleware at all; instead it's
  made available as `context.authorization`, for `perform_*` to inspect once it has whatever
  record-specific info a check actually needs, and reject itself
  (`raise HTTPException(403, ...)`) if it decides to.

```python
from fastapi_viewsets.conf import settings
from fastapi_viewsets.middleware.auth.authorization import Authorization

settings.viewsets_command_middleware = [Authorization()]
```

The two roles map onto `Middleware`'s two methods directly - `depends()` holds only the
reject-capable (callable) case; the always-happens, never-rejects context enrichment stays in
`__call__` (there's no early-timing benefit to it, so it runs at its usual point in the onion
chain, unchanged):

```python
class Authorization(Middleware):
    async def depends(self, request, cls, context) -> None:
        config = self.config_from(context)
        if callable(config):
            allowed = config(request, cls, context)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                raise HTTPException(status_code=403, detail="Not authorized to perform this action")

    async def __call__(self, request, viewset, context, call_next):
        context.set("authorization", self.config_from(context))
        return await call_next()
```

### Middleware-level: no specific record needed

```python
@action_configuration({Authorization: lambda request, cls, context: is_admin(context)})
class AdminOnlyViewSet(...): ...
```

### Method-level: `perform_*` makes the actual call

The middleware only carries a marker into `context.authorization`; `perform_*` decides once it
has fetched the record a real check needs:

```python
@action_configuration({Authorization: "owner-only"})
class ItemViewSet(CollectionViewSet[int, Item]):
    async def perform_retrieve(self, context: Context, pk: int) -> Item:
        item = await self.container_retrieve(pk)
        if await context.authorization == "owner-only" and item.owner != await context.user:
            raise HTTPException(status_code=403, detail="Not your item")
        return item
```

Raising `HTTPException` directly (rather than a `ViewSetResult` short-circuit) works from anywhere
in the pipeline, including inside `perform_*` itself - see
[Action Configuration](./action-configuration#rejecting-a-request-just-raise).

## Why `403`, not `401`

**`403 Forbidden`** - the caller *is* known (whatever "known" means for the anchor a check uses -
usually `context.user`), but isn't allowed to do this specific thing. Contrast with
[Authentication](./authentication#why-401-not-403)'s `401 Unauthorized`, which means the credential
itself doesn't establish an identity at all - there's nothing yet to check permissions against.

## Putting it together with Authentication

The two compose by ordering, not by one depending on the other's code:

```python
from fastapi_viewsets.conf import settings
from fastapi_viewsets.context.auth import auth_context_processor
from fastapi_viewsets.middleware.auth import Session
from fastapi_viewsets.middleware.auth.authorization import Authorization

settings.viewsets_context_processors = [auth_context_processor]   # produces context.user
settings.viewsets_command_middleware = [Session(), Authorization()]  # Session first: reject
                                                                       # before Authorization even
                                                                       # has a real user to check
```

## Known limitations

- `Authorization` only *enforces* anything at the middleware level when its configured value is
  callable - a non-callable value is purely informational (`context.authorization`), and nothing
  forces `perform_*` to actually check it. There's no generic way to guarantee every action
  configured with a non-callable value gets checked somewhere.
