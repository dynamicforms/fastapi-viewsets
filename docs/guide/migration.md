# Migration guide

Every breaking release has its own section below, newest first. If you are crossing several
releases at once, work from the bottom of the page upwards.

<!-- New releases go directly below this comment, above the previous one, as `## Upgrading to vX.Y.Z (from vA.B.x)`. -->

## Upgrading to v0.14.0 (from v0.13.x)

- [ ] A client that sends the `in` or `overlaps` parameter of a `make_filter_model` filter as one
      comma-separated value sends one parameter per value: `?year__in=1999&year__in=2000` instead
      of `?year__in=1999,2000`. The TS client sends an array in this form
      (`list({ year__in: [1999, 2000] })`); a string joined with commas is one value.
- [ ] Code that builds a `make_filter_model` model directly passes a list to an `in` or
      `overlaps` field: `TrackFilter(year__in=[1999, 2000])`.

## Upgrading to v0.13.0 (from v0.12.x)

- [ ] Move an override of `setup_lookup_filter(fltr)` into an override of
      `apply_lookup_filter(context, query, records)`; see [Lookup](./python-mixins#lookup). The filter
      is `query.fltr`; narrow `records` (the source `perform_lookup` returned) instead of state on
      the viewset, and call `query.mark_applied("filter")` when nothing is left for `filter_lookup`:

  ```python
  # 0.12
  async def setup_lookup_filter(self, fltr):
      self._ids = fltr.ids

  async def perform_lookup(self, context):
      return [to_item(tag) for tag in Tag.objects.filter(id__in=self._ids)]

  # 0.13
  async def perform_lookup(self, context):
      return Tag.objects.all()

  async def apply_lookup_filter(self, context, query, records):
      if query.has_filter and query.fltr.ids:
          records = records.filter(id__in=query.fltr.ids)
          query.mark_applied("filter")
      return await super().apply_lookup_filter(context, query, records)

  def to_lookup_item(self, raw):
      return to_item(raw)
  ```

- [ ] Rename a method of a `LookupMixin` viewset named `get_lookup`, `apply_lookup_filter`,
      `land_lookup` or `to_lookup_item` that is not meant as the stage of that name.
- [ ] A BE endpoint, outside the package's mixins, that reads an array query parameter sent by the TS
      REST transport under `name[]` reads it under `name`: the transport sends `ids=1&ids=2`.
- [ ] A hand-written proxy (a subclass of `RestProxyImpl` / `MuxwsProxyImpl`) that overrides `lookup()` takes the
      base signature, `lookup<I = LookupItem>(params?: LookupParams): ViewSetActionCall<I[]>`; the 0.12 signature
      `lookup(): ViewSetActionCall<LookupItem[]>` is TS2416.

## Upgrading to v0.12.0 (from v0.11.x)

- [ ] Replace every comparison with one of the package's error codes by its full code. A code is
      `<namespace>.<code>`; the package's codes are in the namespace `fastapi-viewsets`, exported as
      `ERROR_NAMESPACE` from `fastapi_viewsets` and from `@dynamicforms/fastapi-viewsets`. This covers
      `DfViewSetError.code`, `CursorError.code`, `detail_code` of a response, `error` of a poll's
      `TopicAnswer` and `code` of `toErrorDescription()`:

  | 0.11 | 0.12 |
  |------|------|
  | `not_found` | `fastapi-viewsets.not_found` |
  | `read_only` | `fastapi-viewsets.read_only` |
  | `session_expired` | `fastapi-viewsets.session_expired` |
  | `not_authorized` | `fastapi-viewsets.not_authorized` |
  | `rate_limited` | `fastapi-viewsets.rate_limited` |
  | `result_timeout` | `fastapi-viewsets.result_timeout` |
  | `result_reader_stopped` | `fastapi-viewsets.result_reader_stopped` |
  | `action_dispatch_failed` | `fastapi-viewsets.action_dispatch_failed` |
  | `action_interrupted` | `fastapi-viewsets.action_interrupted` |
  | `action_failed` | `fastapi-viewsets.action_failed` |
  | `idempotency_key_invalid` | `fastapi-viewsets.idempotency_key_invalid` |
  | `idempotency_key_reused` | `fastapi-viewsets.idempotency_key_reused` |
  | `unsupported_list_shape` | `fastapi-viewsets.unsupported_list_shape` |
  | `event_topic_invalid` | `fastapi-viewsets.event_topic_invalid` |
  | `event_topic_refused` | `fastapi-viewsets.event_topic_refused` |
  | `event_scope_missing` | `fastapi-viewsets.event_scope_missing` |
  | `event_subscriptions_exceeded` | `fastapi-viewsets.event_subscriptions_exceeded` |
  | `event_cursors_exceeded` | `fastapi-viewsets.event_cursors_exceeded` |
  | `cursor_unreadable` | `fastapi-viewsets.cursor_unreadable` |
  | `cursor_missing_position` | `fastapi-viewsets.cursor_missing_position` |
  | `cursor_stale` | `fastapi-viewsets.cursor_stale` |
  | `cursor_missing_keys` | `fastapi-viewsets.cursor_missing_keys` |
  | `cursor_value_mismatch` | `fastapi-viewsets.cursor_value_mismatch` |

- [ ] Field errors carry pydantic's `type` as `pydantic.<type>` (`ERROR_NAMESPACE_PYDANTIC`):
      `string_too_short` is `pydantic.string_too_short`.
- [ ] Move the translations of these codes under a key prefix per namespace, and map the namespaces to
      the prefixes through `ERROR_NAMESPACE` and `ERROR_NAMESPACE_PYDANTIC`; see
      [Translating the codes](./error-codes#translating-the-codes).
- [ ] Prefix the codes of the application's own `DfViewSetError` subclasses with a namespace of its
      own. The package passes them on as given.
- [ ] Flush the idempotency records and the pending `celery_viewset` results on upgrade, or let them expire: 0.12
      stores the full code (`fastapi-viewsets.not_found`) and reads a stored code as given, so a record 0.11 wrote
      (an idempotency record within its TTL, a result a 0.11 worker pushed) replays with its bare code
      (`not_found`), and its validation errors and unhandled errors as `fastapi-viewsets.http_error`.

### One error body

Every error of a viewset's route is answered with one body, `ApiErrorBody`, over REST and muxws, without a handler
registered by the application (see [Error codes → The error body](./error-codes#the-error-body)):

```json
{ "detail": "...", "detail_code": "<namespace>.<code>", "detail_params": {}, "field_errors": {} }
```

- [ ] Remove the registration of `df_viewset_exception_handler`; the function no longer exists. Call
      `install_error_handlers(app)` (`fastapi_viewsets.error_responses`) where the application's other routes, and a
      path no route matches, are to answer with the same body:

  ```python
  # before
  from fastapi_viewsets.exceptions import DfViewSetError, df_viewset_exception_handler

  app.add_exception_handler(DfViewSetError, df_viewset_exception_handler)

  # after
  from fastapi_viewsets.error_responses import install_error_handlers

  install_error_handlers(app)
  ```

- [ ] Read the bodies of the viewsets' errors as `ApiErrorBody`. Every body carries all four fields:

  | Error | 0.11 body | 0.12 body |
  |-------|-----------|-----------|
  | `NotFoundError(42)`, handler registered | `{"detail": "Item with pk 42 not found", "detail_code": "not_found", "detail_params": {"pk": 42}}` | `{"detail": "Item with pk 42 not found", "detail_code": "fastapi-viewsets.not_found", "detail_params": {"pk": 42}, "field_errors": {}}` |
  | `NotFoundError(42)`, no handler | `{"detail": "Item with pk 42 not found"}` | the same as above |
  | a request that fails validation | `{"detail": [{"type": "string_too_short", "loc": ["body", "name"], "msg": "String should have at least 3 characters", "input": "a", "ctx": {"min_length": 3}}]}` | `{"detail": "The request failed validation", "detail_code": "fastapi-viewsets.validation_failed", "detail_params": {}, "field_errors": {"name": [{"code": "pydantic.string_too_short", "params": {"min_length": 3}, "detail": "String should have at least 3 characters"}]}}` |
  | `raise HTTPException(403, "Not your item")` | `{"detail": "Not your item"}` | `{"detail": "Not your item", "detail_code": "fastapi-viewsets.http_error", "detail_params": {"status": 403}, "field_errors": {}}` |
  | `raise HTTPException(400, {"field": "x"})` | `{"detail": {"field": "x"}}` | `{"detail": "Bad Request", "detail_code": "fastapi-viewsets.http_error", "detail_params": {"status": 400, "detail": {"field": "x"}}, "field_errors": {}}` |
  | an exception without a handler | plain text `Internal Server Error` | `{"detail": "Internal Server Error", "detail_code": "fastapi-viewsets.unhandled_error", "detail_params": {}, "field_errors": {}}` |
  | a muxws command with headers no route matches | `{"detail": ":path must be absolute, got 'tracks'"}` (400) | `{"detail": ":path must be an absolute path", "detail_code": "fastapi-viewsets.command_invalid", "detail_params": {"reason": "path_relative"}, "field_errors": {}}` (400) |
  | an exception before a muxws command's response | payload `null` (500) | the body of `fastapi-viewsets.unhandled_error` (500) |

  The response of an exception without a handler no longer contains its text; the exception is re-raised and logged
  by the server as before. A handler the application registered for an exception class, a status, `HTTPException`,
  or `Exception` / 500 keeps answering as it did.
- [ ] Translate the new codes: `fastapi-viewsets.validation_failed`, `fastapi-viewsets.http_error` (by
      `params.status`), `fastapi-viewsets.unhandled_error`, `fastapi-viewsets.command_invalid`, and the client's
      `fastapi-viewsets.no_response` and `fastapi-viewsets.unexpected_response` (see
      [Error codes → Codes](./error-codes#codes)).
- [ ] An authorizer's `HTTPException` without a code refuses a topic of a poll with `fastapi-viewsets.http_error`
      (`params.status`), no longer `fastapi-viewsets.event_topic_refused`.
- [ ] `TopicAnswer.error` of a poll is the refusal's `ApiErrorBody`, no longer its code:

  ```jsonc
  // 0.11
  {"rev": null, "events": null, "error": "fastapi-viewsets.event_topic_refused", "status": 403}
  // 0.12
  {"rev": null, "events": null, "error": {"detail": "Topic org:9 is not admitted", "detail_code": "fastapi-viewsets.event_topic_refused", "detail_params": {"topic": "org:9"}, "field_errors": {}}, "status": 403}
  ```

  The event client reports it as a `ViewSetRequestError` of `status` whose `response.data` is that body. Deploy the
  frontend and the backend together: a 0.12 event client leaves a topic answered with a code string unanswered, and
  a 0.11 one leaves a topic answered with a body unanswered.
- [ ] Frontend: `ApiErrorBody` is the body above, with every field required. Replace `FieldErrorEntry` and
      `FieldErrorsBody` with `ApiErrorBody` and `ApiFieldError`; `toFieldErrors()` takes an `ApiErrorBody` and reads
      its `field_errors`. Take the body of a failed call with `errorBodyOf(error)`, which also describes a call the
      BE did not answer:

  ```ts
  // before
  const body = error.response?.data;
  if (error.response?.status === 422) toFieldErrors(body as FieldErrorsBody);
  else if (typeof body?.detail === 'string') toErrorDescription(body as ApiErrorBody);

  // after
  const body = errorBodyOf(error);
  toFieldErrors(body); // {} for an error without field errors
  toErrorDescription(body);
  ```

- [ ] OpenAPI: every operation of a viewset documents 422, 500 and `default` with the schema `ApiErrorBody`, and
      the operations that address an item by `pk` 404; `HTTPValidationError`, `ValidationError` and
      `NotFoundResponse` are gone from the document. Regenerate a client generated from it.
- [ ] A field error of a type pydantic does not declare (an application's `PydanticCustomError`) is
      `pydantic.custom_error` with `params` `{type, ctx}`; the primary key in a create's body is
      `fastapi-viewsets.pk_assigned_by_server`.

## Upgrading to v0.11.0 (from v0.10.x)

- [ ] Pass `redis_client` (a synchronous `redis.Redis`) to `celery_viewset` and
      `celery_viewset_server` in the worker too; without it the decorator raises `ValueError`.
- [ ] Deploy the FastAPI processes and the Celery workers together. A 0.10 FastAPI process reads a
      0.11 worker's error for a non-HTTP failure as a bare `Exception`; a 0.11 process reads a 0.10
      worker's error as before.
- [ ] Code in `Middleware.execute()` or in an action that catches billiard's
      `SoftTimeLimitExceeded`, or `asyncio.CancelledError` for a time limit, catches
      `ActionInterruptedError` (reason `time_limit`) from `fastapi_viewsets.exceptions`. The action
      itself still sees `asyncio.CancelledError` where it was suspended at an `await`.
- [ ] Code in `__call__` that caught a bare `Exception` from a deferred action catches
      `ActionFailedError` (500, `action_failed`); its `detail` names the task only, and the original
      error is logged in the worker. Code that waited for `ResultTimeoutError` after a hard time
      limit, a termination or a lost worker process catches `ActionInterruptedError`. Code that
      treats the two "stopped waiting" errors alike catches `ActionOutcomeUnknownError`.
- [ ] Add the codes `action_interrupted` and `action_failed` to the application's error
      translations, and read `params.reason` where the reason matters; see
      [Error codes](./error-codes#executor-outcomes).
- [ ] Move per-task time limits out of Celery's `task_annotations` (or `soft_time_limit` /
      `time_limit` on the tasks) into `@action_configuration({TimeLimits: TimeLimits(soft=, hard=)})`;
      see [Time limits](./routers#time-limits). Limits in the worker's command line still apply to
      an action without a `TimeLimits` entry.
- [ ] Setup a middleware did in a handler of Celery's `worker_process_init` (or `worker_init`)
      moves into [`Middleware.executor_started()`](./command-middleware#setting-up-the-worker-middleware-executor-started),
      and code that watched Celery's `task_failure`, `task_revoked`, `task_unknown` or
      `task_rejected` for `celery_viewset` tasks moves into
      [`Middleware.on_transition()`](./command-middleware#transitions-outside-the-execution-middleware-on-transition).
- [ ] An application that relies on `task_acks_late` or `reject_on_worker_lost` for
      `celery_viewset` tasks: they are registered with `acks_late=False` and
      `reject_on_worker_lost=False`, and a call is never delivered twice. A call whose worker
      process is lost ends with `ActionInterruptedError` (reason `lost`).
- [ ] A test that called a `celery_viewset_server` task and expected the action's own non-HTTP
      exception expects `ActionFailedError`, with the original as `__cause__`.

## Upgrading to v0.10.0 (from v0.9.x)

- [ ] Deploy every process with a `RedisEventStore`, web processes and workers, together:
      `publish` publishes on `{prefix}events` instead of `{prefix}ch:{topic}`, and a reader of one
      version does not receive the events a process of the other version publishes.
- [ ] A custom `EventStore` drops `watch` and `unwatch`; nothing calls them. Its `publish` carries
      every event to `deliver()` in every process whose store is started, and `deliver()` leaves out
      a topic without a subscription in the process.
- [ ] Code that called `RedisEventStore.channel(topic)` reads `RedisEventStore.channel`, the name of
      the one channel.
- [ ] `start_events()` raises `asyncio.TimeoutError` when Redis does not confirm the SUBSCRIBE
      within 5 seconds; the lifespan that calls it fails the application's startup.

## Upgrading to v0.9.0 (from v0.8.x)

- [ ] `set_celery_dispatch_hook` and `set_celery_kwargs_hook` are removed. Code that carried data to
      the worker through them puts the data into the context with a context processor; the context
      reaches the worker for every action. Code that wrapped the execution in the worker moves into
      [`Middleware.execute()`](./command-middleware#around-the-execution-middleware-execute), and the
      worker process sets the same `settings.viewsets_command_middleware`. Code that watched the
      broker publish catches `ActionDispatchError` in `__call__`.
- [ ] Deploy the FastAPI processes and the Celery workers together: the task's kwargs changed (the
      context is sent as `_context`, with `_request` and `_parent` beside it).
- [ ] A test that calls a `celery_viewset_server` task with `context={...}` passes
      `_context={...}` instead, or nothing for an empty context.
- [ ] Add the code `action_dispatch_failed` to the application's error translations; see
      [Error codes](./error-codes#codes). A call whose task could not be sent raised the error of
      `send_task` before; it raises `ActionDispatchError` (503) with that error as `__cause__`.
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
- [ ] npm: a muxws proxy's `timeoutMs` bounds one attempt; an attempt that reaches it is repeated, up
      to `repeat.withinMs`, instead of rejecting the call with `StreamTimeout`. Pass
      `repeat: { withinMs: 0 }` to reject on the first timeout.
- [ ] An application using muxws upgrades it to 0.4.0: `muxws>=0.4.0` (with `websockets>=14`) in
      Python, `muxws@^0.4.0` on npm. See the muxws migration guide for its own changes.
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
