# Migration guide

Every breaking release has its own section below, newest first. If you are crossing several
releases at once, work from the bottom of the page upwards.

<!-- New releases go directly below this comment, above the previous one, as `## Upgrading to vX.Y.Z (from vA.B.x)`. -->

## Upgrading to v0.9.0 (from v0.8.x)

- [ ] muxws: cancelling a stream, or losing the socket, no longer cancels the request it carries; the
      request runs to completion and its answer is discarded. An application that relied on
      cancelling the stream to stop an action stops it through the application instead.
- [ ] To make calls repeatable, add `Idempotency()` (with `redis_client=` for more than one process)
      as the first entry of `settings.viewsets_command_middleware`; see
      [Idempotent Calls](./idempotency). Add the codes `idempotency_key_invalid` and
      `idempotency_key_reused` to the application's error translations; see
      [Error codes](./error-codes#codes).
- [ ] npm: the proxies repeat a call that gets no answer, with the same `Idempotency-Key`, and a REST
      attempt times out after 30 s. Add `Idempotency` on the backend, or pass
      `repeat: { withinMs: 0 }` (and `attemptTimeoutMs: 0` to keep REST calls without a timeout) to
      every proxy: without the middleware a repeat runs the action again. A REST call that
      legitimately takes longer than 30 s needs a larger `attemptTimeoutMs`, or `Idempotency` on the
      backend, which attaches the repeat to the running call.
- [ ] npm: a REST backend on another origin adds `Idempotency-Key` to its CORS allowed headers.
- [ ] npm: code that calls the proxy's methods by `Promise<R>` is unaffected; `ViewSetCall<R>` is a
      subtype. A transport of one's own that extends `ViewSetProxyBase` returns
      `issueCall(promise, key)` from `request()`, with the key from `options.key ?? newCallKey()`
      sent as `Idempotency-Key: idempotencyKeyHeader(key)`.
- [ ] npm: a test that asserts the exact arguments of an axios mock includes the config every call
      now carries: `headers` with `Idempotency-Key`, `timeout` and `signal`.
- [ ] npm: an application that repeats a call itself passes the first call's `key` as
      `RequestOptions.key` from a custom method, instead of letting the repeat get a new key.

## Upgrading to v0.8.0 (from v0.7.x)

The npm package no longer translates errors and no longer depends on `@dynamicforms/translatable`.
It converts an error body into an `ErrorDescription`, and the application translates it by its
`code` with the function it uses for its vue-forms errors. The response bodies are unchanged.

- [ ] Remove `@dynamicforms/translatable` from the application's dependencies if nothing else uses
      it.
- [ ] Remove the `translateStrings` call.
- [ ] Replace `translateApiError(body)` with the application's error text function over
      `toErrorDescription(body)`:

  ```ts
  // before
  translateStrings((key) => myTranslations[key]);
  const message = translateApiError(body);

  // after
  const message = errorText(toErrorDescription(body));
  ```

  `errorText` is the application's function described in
  [vue-forms: Error messages and translation](:vue-forms:/api/validators.html#error-messages-and-translation).
  It looks the code up in the application's translations, substitutes `params` and falls back to
  `detail`. Add the codes listed in [Error codes](./error-codes#codes) to the translations; the
  placeholder names are the same.
- [ ] Remove reads of `translatableStrings`. The English text of each code is the body's `detail`.
- [ ] `params` holds the values as the server sent them. `allowed` and `missing` are arrays; format
      them in `errorText` where the translation shows them.
- [ ] muxws: a command goes through the application that serves the WebSocket endpoint, with its
      middleware, exception handlers and router dependencies. Check what of these now applies to
      muxws as well: a dependency on the `APIRouter` handed to `route_viewset` now guards muxws
      commands, and an HTTP middleware now sees them.
- [ ] muxws: a command's `:path` includes the prefix of `app.include_router(router, prefix=...)`.
      Give a muxws ViewSet the same `basePath` as its REST counterpart.
- [ ] muxws: pass `connection=websocket` (or `app=`) to every `process_command` call; a call with
      neither raises `ValueError`. Remove uses of `get_dispatch_app()`; a test that sent commands
      into it passes its application as `app=`.
- [ ] Code that reads `router.routes` after `route_viewset` finds the routes published on muxws
      only there as well; a route's `transports` says which transports it answers.
- [ ] Deploy the FastAPI process and the Celery workers together when the application uses
      `JWTAuthBackend` with `celery_viewset`: the user crosses to the worker as the token and the
      position of its backend in `settings.viewsets_auth_processors`, which the worker reads from
      its own settings. A worker of 0.7.x cannot read the new recipe, and a 0.8.0 worker cannot read
      the old one.
- [ ] A `perform_update` that applies a `PATCH` reads the fields the request stated from
      `data.model_fields_set` (with `getattr`, which keeps a nested model as the model); the body no
      longer carries the primary key or the fields it left out.
- [ ] `DjangoORMViewSet`: an update validates the stored row with the request's fields set on it,
      `@model_validator`s included. Rows stored in breach of the schema's `@model_validator`s
      answer 422 to every update that leaves them in breach; find them by reading each row through
      `schema` and correct them.
- [ ] `DjangoORMViewSet`: `save()` and `pre_save`/`post_save` handlers run inside the update's
      transaction. A handler that starts work reading the row over another connection (a Celery
      task) starts it with `transaction.on_commit`.
- [ ] A ViewSet with custom endpoints only can be built with `restViewSet<T>()(pk, [])`; the schema
      check matches custom endpoints by the BE method's name (`top_rated` → `topRated()` or
      `top_rated()`), so a FE method named after a hyphenated path segment is reported until it is
      renamed.

## Upgrading to v0.6.0 (from v0.5.7)

Nothing about a response body changes unless you opt in. There is no checklist for this release -
see [Error codes](./error-codes) for what changed and how to opt into it.

`NotFoundError` moves from `fastapi_viewsets.response_classes` to `fastapi_viewsets.exceptions`:

```python
# before
from fastapi_viewsets.response_classes import NotFoundError

# after
from fastapi_viewsets.exceptions import NotFoundError
```
