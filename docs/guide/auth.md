# Auth

Pluggable authentication: a chain of credential-recognizing backends contributing `context.user`,
plus a command middleware that rejects requests whose session is missing or expired before
`perform_*` ever runs. Built entirely on the general [Context Processors](./context-processors) and
[Command Middleware](./command-middleware) mechanisms - nothing here is special-cased by the
library outside of `fastapi_viewsets.context.auth`.

::: tip Where this fits
See [Architecture](./architecture#problem-real-auth-has-more-than-one-way-to-prove-who-you-are) for
why this exists and how it grew out of a single ad-hoc context processor.
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

### `StaticUserAuthBackend` - ad-hoc, fixed users

For prototyping/tests: a fixed mapping of session token → user data (e.g. loaded from a small JSON
file), no real session store involved.

```python
from fastapi_viewsets.context.auth.static import StaticUserAuthBackend

backend = StaticUserAuthBackend({"tok-jure": {"id": 1, "username": "jure"}})
# or load from disk - the file should contain a JSON object mapping token -> user data:
backend = StaticUserAuthBackend.from_json_file("users.json")

settings.viewsets_auth_processors = [backend]
```

Requests carry the token in the `X-Session-Token` header by default (configurable via
`header_name=`). Resolution is synchronous and needs no I/O - `context.user` is available the
moment anything awaits it.

### `DjangoSessionAuthBackend` - real Django sessions

Resolves the caller from a real Django session, the same way Django's own
`AuthenticationMiddleware` does (`django.contrib.auth.get_user`, which validates the session's auth
hash - a changed password invalidates the session exactly as it would for a real Django request) -
except the session key arrives via an `X-Session-Token` header instead of Django's own session
cookie, so it works for non-browser/cross-origin clients too.

```python
from fastapi_viewsets.context.auth.django import DjangoSessionAuthBackend

settings.viewsets_auth_processors = [DjangoSessionAuthBackend()]
```

Uses whichever backend `django.conf.settings.SESSION_ENGINE` names (DB, cache, Redis, ...), same as
Django itself. Requires the `django` extra (`pip install "dynamicforms-fastapi-viewsets[django]"`)
and a Django app already configured/`django.setup()`'d by the host application - this backend
doesn't configure Django itself, it only uses whatever's already set up. Resolution runs the actual
Django ORM/session lookup through `asgiref.sync.sync_to_async`, so the event loop isn't blocked.

Because the resolved value is a live Django ORM `User` instance - not JSON-safe - this backend's
`LazyObject` always serializes as just its recipe (the `session_key`); a Celery worker re-resolves
it independently (one extra query, but correct) rather than receiving something un-picklable. See
[Context Processors](./context-processors#lazyobject) for what "recipe" means here.

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
    async def __call__(self, request, viewset, context, call_next):
        config = self.config_from(context)
        required = True if config is None else bool(config)
        if not required:
            return await call_next()
        user = await context.user
        if user is None:
            return ViewSetResult(body={"detail": "Session expired or invalid"}, status_code=401)
        return await call_next()
```

`Session` is a class rather than a plain function specifically so it can live as reusable library
code under `fastapi_viewsets.middleware.auth` (see [Command Middleware](./command-middleware) for
`Middleware`, the small ABC it implements) - functionally it's just a `CommandMiddleware` like any
other. With this wired in alongside `auth_context_processor`, a request with no recognized/valid
session gets a `401` before `perform_*` ever runs; a request with a valid one passes through
untouched.

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

- **`401 Unauthorized`** - the credential itself no longer establishes who's calling: missing,
  garbage, or expired. That's exactly the case here.
- **`403 Forbidden`** - the caller *is* known, but isn't allowed to do this specific thing (a
  permissions/authorization check on an already-verified identity). Doesn't apply to a rejected
  session, since there's no verified identity yet to check permissions against.

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

- Both backends recognize credentials via an `X-Session-Token` header - there's no built-in cookie-
  or `Authorization: Bearer`-based backend yet. Write your own `AuthBackend` for those (see
  [Writing your own backend](#writing-your-own-backend)).
- `auth_context_processor`/`Session` are ordinary context processor/command middleware entries - if
  your app already configures other processors or middleware, add these alongside them (order among
  *context processors* doesn't matter for auth; `auth_context_processor` and any other processor
  writing to `context.user` will just have the later one win on collision).
