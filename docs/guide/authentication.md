# Authentication

Pluggable authentication: a chain of credential-recognizing backends contributing `context.user`,
plus a command middleware that rejects requests whose session is missing or expired before
`perform_*` ever runs. Built entirely on the general [Context Processors](./context-processors) and
[Command Middleware](./command-middleware) mechanisms - nothing here is special-cased by the
library outside of `fastapi_viewsets.context.auth`/`fastapi_viewsets.middleware.auth`. This is one
of a few [built-in middleware/processor implementations](./rate-limiter) this library ships -
[Authorization](./authorization) is a separate, independent one that happens to pair well with it.

::: tip Where this fits
See [Architecture](./architecture#problem-real-auth-has-more-than-one-way-to-prove-who-you-are) for
why this exists and how it grew out of a single ad-hoc context processor, and
[Architecture](./architecture#problem-a-context-processor-can-t-say-no) for why rejecting an
expired session needed command middleware (`Session`, below) rather than the context processor
itself.
:::

---

## `AuthBackend`

One entry in `settings.viewsets_auth_processors`. Each backend recognizes a different way of
proving who's calling:

```python
from abc import ABC, abstractmethod
from fastapi import Request
from fastapi_viewsets.context import LazyObject

class AuthBackend(ABC):
    @abstractmethod
    def try_handle(self, request: Request) -> LazyObject | None:
        """Cheap, synchronous check. Return None immediately if this backend doesn't recognize
        credentials in this request (the caller tries the next backend). Otherwise return a
        LazyObject that resolves (sync or async) to the authenticated user, or None if the
        credential turns out invalid/expired once actually resolved."""
```

`try_handle` returning non-`None` **claims** the request - no other backend is tried after that,
even if the claimed `LazyObject` later resolves to `None` (an invalid/expired credential of a
*recognized* shape is not the same as "this isn't my kind of credential, try the next backend").

## `auth_context_processor`

A ready-to-use `settings.viewsets_context_processors` entry that iterates
`settings.viewsets_auth_processors` and uses the first backend that claims the request:

```python
from fastapi_viewsets.conf import settings
from fastapi_viewsets.context.auth import auth_context_processor

settings.viewsets_context_processors = [auth_context_processor]
settings.viewsets_auth_processors = [my_backend_a, my_backend_b]
```

If no configured backend claims the request (or none are configured at all), `context.user`
resolves to `None`. This processor only ever *gathers* that fact - see
[`Session`](#rejecting-unauthenticated-requests-session) below for the piece that actually rejects
the request because of it.

## Backends

### `StaticUserAuthBackend`/`StaticUserCookieAuthBackend` - ad-hoc, fixed users

For prototyping/tests: a fixed mapping of session token → user data (e.g. loaded from a small JSON
file), no real session store involved. Two backends, differing only in *where* the token comes
from - register one, the other, or both (in whichever order fits your traffic):

```python
from fastapi_viewsets.context.auth.static import StaticUserAuthBackend, StaticUserCookieAuthBackend

users = {"tok-jure": {"id": 1, "username": "jure"}}
# or load from disk - the file should contain a JSON object mapping token -> user data:
# users = StaticUserAuthBackend.from_json_file("users.json").users_by_token

settings.viewsets_auth_processors = [
    StaticUserCookieAuthBackend(users),   # reads a cookie (default name: "sessionid")
    StaticUserAuthBackend(users),         # reads the X-Session-Token header
]
```

`header_name=`/`cookie_name=` override the default names on either. Resolution is synchronous and
needs no I/O - `context.user` is available the moment anything awaits it.

### `DjangoSessionAuthBackend`/`DjangoSessionCookieAuthBackend` - real Django sessions

Resolves the caller from a real Django session, the same way Django's own
`AuthenticationMiddleware` does (`django.contrib.auth.get_user`, which validates the session's auth
hash - a changed password invalidates the session exactly as it would for a real Django request).
Two backends, same underlying resolution, differing only in *where* the session key comes from:

- **`DjangoSessionAuthBackend`** - an `X-Session-Token` **header**, for non-browser/cross-origin
  clients (a mobile app, a service-to-service call, allauth's headless "app" client mode).
- **`DjangoSessionCookieAuthBackend`** - Django's own session **cookie** (default name: whatever
  `django.conf.settings.SESSION_COOKIE_NAME` is, normally `"sessionid"`), for real browser clients
  (allauth's headless "browser" client mode, or plain Django/allauth). This is the one that
  actually works with a standard `HttpOnly` cookie - unlike a header, a cookie is attached to the
  request automatically by the browser, and (being `HttpOnly`) can't be read by JavaScript to be
  copied into a header even if you wanted to.

Register whichever you need, or both to accept either:

```python
from fastapi_viewsets.context.auth.django import DjangoSessionAuthBackend, DjangoSessionCookieAuthBackend

settings.viewsets_auth_processors = [DjangoSessionCookieAuthBackend(), DjangoSessionAuthBackend()]
```

Uses whichever backend `django.conf.settings.SESSION_ENGINE` names (DB, cache, Redis, ...), same as
Django itself. Requires the `django` extra (`pip install "dynamicforms-fastapi-viewsets[django]"`)
and a Django app already configured/`django.setup()`'d by the host application - these backends
don't configure Django themselves, they only use whatever's already set up. Resolution runs the
actual Django ORM/session lookup through `asgiref.sync.sync_to_async`, so the event loop isn't
blocked.

Because the resolved value is a live Django ORM `User` instance - not JSON-safe - both backends'
`LazyObject` always serializes as just its recipe (the `session_key`); a Celery worker re-resolves
it independently (one extra query, but correct) rather than receiving something un-picklable. See
[Context Processors](./context-processors#lazyobject) for what "recipe" means here.

For the rest of what a Django-backed project needs beyond auth - initializing Django itself in
both the FastAPI and Celery worker processes, `autodiscover_tasks`, and the recommended ORM-access
convention for `perform_*` - see [Django Integration](./django-integration).

### `JWTAuthBackend` - stateless `Bearer` tokens

Verifies a JWT's signature and expiry - no session store, no database lookup, purely local
computation. Requires the `jwt` extra (`pip install "dynamicforms-fastapi-viewsets[jwt]"`).

```python
from fastapi_viewsets.context.auth.jwt import JWTAuthBackend

settings.viewsets_auth_processors = [JWTAuthBackend(secret="...", algorithm="HS256")]
```

**Not a session replacement.** A bare, stateless JWT can't be revoked - there's no server-side
record to delete, unlike `DjangoSessionAuthBackend`. If "logout"/password-change/force-logout
matters, don't hand out long-lived JWTs directly; instead mint a *short-lived* one (`encode_jwt`,
also in `fastapi_viewsets.context.auth.jwt`) only once an actual, revocable session (e.g. a Django
session, via `DjangoSessionAuthBackend`) is confirmed active:

```python
from fastapi_viewsets.context.auth.jwt import encode_jwt

# inside your own login/token-exchange endpoint, after confirming an active Django session:
access_token = encode_jwt({"sub": str(user.id)}, secret="...", expires_in=datetime.timedelta(minutes=15))
```

`Session`/logout still revoke the underlying Django session as usual; already-issued JWTs simply
expire on their own, soon, being short-lived - real revocation lives entirely in the session layer,
the JWT is just a fast, stateless-to-verify derivative of it.

### Writing your own backend

Any class implementing `try_handle(request) -> LazyObject | None` works - it doesn't have to
subclass `AuthBackend`'s concrete behavior beyond the contract itself:

```python
class ApiKeyAuthBackend(AuthBackend):
    def try_handle(self, request: Request) -> LazyObject | None:
        api_key = request.headers.get("x-api-key")
        if api_key is None:
            return None  # not my kind of credential - let the next backend try
        return _ApiKeyUserLazy(api_key)
```

Multiple backends run in order - list the more specific/faster ones first:

```python
settings.viewsets_auth_processors = [StaticUserAuthBackend(...), DjangoSessionAuthBackend()]
```

## Rejecting unauthenticated requests: `Session`

A context processor can only ever gather the fact that `context.user` is `None` - it can't reject
the request or touch the response (see
[Architecture](./architecture#problem-a-context-processor-can-t-say-no)). That's what this command
middleware does - it operates purely on the already-resolved `context.user` that
`auth_context_processor` produced, it doesn't re-derive or re-resolve anything itself:

```python
from fastapi_viewsets.conf import settings
from fastapi_viewsets.middleware.auth import Session

settings.viewsets_command_middleware = [Session()]
```

```python
class Session(Middleware):
    async def depends(self, request, cls, context) -> None:
        config = self.config_from(context)
        required = True if config is None else bool(config)
        if not required:
            return
        user = await context.user
        if user is None:
            raise HTTPException(status_code=401, detail="Session expired or invalid")

    async def __call__(self, request, viewset, context, call_next):
        return await call_next()
```

The whole check lives in `depends()` - bridged onto FastAPI's own native `Depends()` by
`route_viewset` (see [Command Middleware](./command-middleware#early-rejection-middleware-depends)),
so a missing/expired session is rejected before FastAPI even parses the request body; `__call__`
(the onion command-middleware chain) is a trivial passthrough, since `depends()` already made the
only decision that matters. With this wired in alongside `auth_context_processor`, a request with
no recognized/valid session gets a `401` before `perform_*` ever runs; a request with a valid one
passes through untouched.

### Opting a viewset out: `@action_configuration({Session: False})`

`settings.viewsets_command_middleware` is global - it wraps every request-facing execution. A
viewset (or a specific `perform_*` method) that must stay reachable without a session - a
login/signup endpoint, say - opts out via
[`@action_configuration`](./action-configuration), keyed by the `Session` class itself:

```python
from fastapi_viewsets.action_configuration import action_configuration
from fastapi_viewsets.middleware.auth import Session

@route_viewset(router, base_path="/auth")
@action_configuration({Session: False})
class LoginViewSet(...): ...
```

Every other viewset is protected by default - `Session.config_from(context)` returns `None` when
nothing was configured, which `Session` treats as "required". Since `@action_configuration` also
works on an individual method, a single otherwise-protected viewset can carve out one public
action instead of the whole class:

```python
class ItemViewSet(...):
    @action_configuration({Session: False})
    async def perform_list(self, context): ...   # public - other actions on this viewset still require a session
```

### Why `401`, not `403`

**`401 Unauthorized`** - the credential itself no longer establishes who's calling: missing,
garbage, or expired. That's exactly the case here. This is *not* the same question
[Authorization](./authorization) asks (`403 Forbidden` - the caller *is* known, but isn't allowed
to do this specific thing) - a rejected session has no verified identity yet to even check
permissions against.

## Putting it together

```python
from fastapi_viewsets.conf import settings
from fastapi_viewsets.context.auth import auth_context_processor
from fastapi_viewsets.context.auth.static import StaticUserAuthBackend
from fastapi_viewsets.context.auth.django import DjangoSessionAuthBackend
from fastapi_viewsets.middleware.auth import Session

settings.viewsets_context_processors = [auth_context_processor]
settings.viewsets_command_middleware = [Session()]
settings.viewsets_auth_processors = [
    StaticUserAuthBackend.from_json_file("static_users.json"),
    DjangoSessionAuthBackend(),
]
```

```python
class ItemViewSet(CollectionViewSet[int, Item]):
    async def perform_list(self, context: Context) -> list[Item]:
        user = await context.user  # guaranteed non-None here - Session already rejected otherwise
        return [i for i in database.values() if i.owner == user["username"]]
```

## Known limitations

- `StaticUserAuthBackend`/`DjangoSessionAuthBackend` (header) and `StaticUserCookieAuthBackend`/
  `DjangoSessionCookieAuthBackend` (cookie) cover the `X-Session-Token`/session-cookie cases;
  `JWTAuthBackend` covers `Authorization: Bearer`. Anything else (a differently-named cookie
  scheme, a custom signing scheme) still needs your own `AuthBackend` (see
  [Writing your own backend](#writing-your-own-backend)).
- `auth_context_processor`/`Session` are ordinary context processor/command middleware entries - if
  your app already configures other processors or middleware, add these alongside them (order among
  *context processors* doesn't matter for auth; `auth_context_processor` and any other processor
  writing to `context.user` will just have the later one win on collision).
