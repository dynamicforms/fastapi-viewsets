# Changelog

All notable changes to `fastapi-viewsets` — published as `dynamicforms-fastapi-viewsets` on PyPI
and `@dynamicforms/fastapi-viewsets` on npm — will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.2] - 2026-10-16

### Added

- npm: `onCallActivity(listener)` reports every call issued through a proxy as `start`, `retry` and
  `settle` events under its key; `RequestOptions.background` marks a call a busy indicator leaves
  out; `pendingCalls()` and `pendingCallCount()` list and count the calls that have not settled;
  `retryNow()` sends every waiting repeat at once. `issueCall()` takes `background` as a third
  argument.
- npm: the event client sends a check poll of every subscribed topic over the open peer once per
  `checkIntervalMs` (`DEFAULT_CHECK_INTERVAL_MS`, 60 s; `0` sends none) while the ladder is held,
  so a lost push of a topic's last event is caught without a later event of the topic.

### Changed

- npm: `watch({ fetch: true })` passes the changes whose `retrieve` answers 404 to a second
  `onChange` with `op: 'deleted'`, after the event's other changes, instead of dropping them, so a
  displayed record that is gone or no longer visible to the reader is removed.
- `Idempotency` extends a running record to `viewsets_idempotency_lock_ttl` every third of the lock
  TTL while the call runs, and the `:attached` key with it. A call that runs longer than the lock TTL
  keeps its record, and a repeat attaches to it instead of running the action again; the record
  expires only once the process running the call stops extending it.

### Fixed

- Events: `poll` runs `viewsets_event_delivery_filters` on every event it answers, over REST and
  over muxws, and leaves out the events a filter refuses. Each polled event's `prev` is the rev of
  the event before it in the answer, the cursor for the first; the answer's `rev` covers the events
  left out. `Subscription.peer` is the socket a muxws poll arrived on, `None` for a poll over REST.
  `passes_filters()` runs the filters. An event a filter refused on its push is no longer delivered
  by the gap poll that the next push triggers, and a REST-only client receives the same events as a
  subscribed socket.
- npm: the event client applies a polled event whose `prev` is below the held rev and whose rev is
  above it, and moves the held rev to a poll answer's `rev` after its events, so a poll answer that
  leaves out filtered events neither resyncs the topic nor polls it again.
- Events: a `store.watch` that raises, such as a Redis SUBSCRIBE whose confirmation does not arrive
  in time, is followed by `store.unwatch`, so a failed `subscribe` leaves no channel subscribed in
  the process. `EventStore.unwatch` stops what a `watch` that raised may have started.

## [0.9.1] - 2026-10-16

### Added

- Events, `fastapi_viewsets.events` and the npm event client: the backend publishes an event to a
  topic, and every frontend following the topic receives it as a muxws push or on its next poll.
  - A viewset's topic is its routed base path; custom topics are declared with `source()`.
  - Every event is written to a store before it is pushed: `MemoryEventStore` for one process,
    `RedisEventStore` for several. Each event carries a `rev`, the `prev` it follows and an
    `event_version`; a frontend that misses events polls the short log or re-reads its state.
  - `register_events()` serves `subscribe`, `unsubscribe` and `poll`. Authorizers admit topics
    (`ViewsetTopics()` applies the viewset's own checks); delivery filters decide per socket.
  - `ChangeEvents`, a command middleware, publishes the pks of every create, update and destroy of
    a viewset, globally or per viewset, also from a Celery worker.
  - npm: `createEventClient()`, `defineEvent()`, `watch()` on every ViewSet with `applyChanges()`,
    and the polling ladder the client polls with.
  - See [Events](https://docs.velis.si/dynamicforms/fastapi-viewsets/guide/events) and
    [Events (Vue)](https://docs.velis.si/dynamicforms/fastapi-viewsets/guide/vue-events).
- `process_command(..., peer=)` puts the socket in the request scope.
- The demo's grid follows its viewset in every open tab.

### Changed

- `CollectionViewSet.pk_field_name` is the viewset's `pk_field`; the cursor list shape orders by it.
- The `muxws` floor is 0.4.1 on PyPI and npm.
- npm: the positional form of `route_rest` returns `ViewSetCalls<M>`, without `watch()`.

## [0.9.0] - 2026-10-16

### Added

- `fastapi_viewsets.middleware.idempotency.Idempotency(redis_client=None, key_prefix="idempotency:")`:
  a command middleware that answers a call repeated with the same `Idempotency-Key` (a Structured
  Field string, `draft-ietf-httpapi-idempotency-key-header-07`) with the outcome of the first call.
  A repeat of a running call attaches to it. Records live in the instance, or in Redis with
  `redis_client`. `GET` and `HEAD` outcomes are stored only when a repeat attached; an outcome that
  sets a cookie is never stored.
- `parse_idempotency_key`, `read_idempotency_key`, `request_fingerprint` and
  `IDEMPOTENCY_KEY_HEADER` in `fastapi_viewsets.middleware.idempotency`.
- `settings.viewsets_idempotency_ttl` (seconds, default 86400) and
  `settings.viewsets_idempotency_lock_ttl` (seconds, default 600).
- `IdempotencyKeyInvalidError` (400, `idempotency_key_invalid`) and `IdempotencyKeyReusedError`
  (422, `idempotency_key_reused`, param `key`).
- `Middleware.execute(execution, call_next)`: an optional onion layer around the call of the viewset
  method, below every `__call__`, run once per execution where the action executes: in the FastAPI
  process for an in-process action, in the Celery worker for a `celery_viewset` action. Default:
  passes through.
- `ActionExecution` (`viewset_class`, `viewset`, `action`, `args`, `kwargs`, `context`, `request`,
  `parent`) and `ExecutionRef` (`viewset_class`, `action`, `parent`) in `fastapi_viewsets.middleware`.
- `ActionDispatchError` (503, `action_dispatch_failed`, param `task`): a `celery_viewset` call whose
  task could not be sent. `Idempotency` does not store it; a repeat runs the call again.
- A `celery_viewset` task carries the call's context whether or not the action declares `context`,
  the call's request scope without the body, and the execution it was dispatched in (`_context`,
  `_request`, `_parent`). An action declaring `request` receives a `Request` built from the scope in
  the worker. A call made inside another action's execution carries that execution's context and
  request unless it passes a `context` of its own.
- A `celery_viewset` task interrupted by Celery (a time limit) while the action awaits is cancelled
  and run until it has unwound, so its `finally` blocks and `execute()` see the interruption.
- npm: every call through a proxy, REST and muxws, sends `Idempotency-Key`, a new UUID per call as a
  Structured Field string. `RequestOptions.key` sets it for one call.
- npm: `ViewSetCall<R>`, a `Promise<R>` with the call's `key`. `request()` returns it, and every
  method of a proxy sets `key` on its promise when it issues a request before its first `await`.
  `ViewSetActionCall<R>` (the type of a declared action, `key` optional) and `ViewSetCalls<M>`.
- npm: a call whose attempt ends without an answer is repeated with the same key and with the headers
  of its first attempt — REST: an axios error without `response`, or `502`, `503`, `504` whose body
  is not a JSON object with `detail`; muxws: `ConnectionLost`, `StreamTimeout`, `StreamRefused`, and
  `ConnectionClosed` or `ConnectionGoingAway` thrown by `open()` while the peer has not closed for
  good, after the peer has reconnected. The repeated attempt is aborted (REST) or its stream
  cancelled (muxws). Options `repeat: RepeatOptions` (`withinMs`, default 600000, `0` turns
  repeating off; `delayMs`, default 1000, doubled per repeat up to 30000) on every proxy and on
  `RequestOptions`, and `attemptTimeoutMs` (default 30000) on REST proxies.
- npm: `IDEMPOTENCY_KEY_HEADER`, `newCallKey()`, `idempotencyKeyHeader(key)` and
  `issueCall(promise, key)`.
- npm: `MuxwsStreamLike.cancel`, `MuxwsStreamLike.headers` and `MuxwsPeerLike.isOpen`,
  `onReconnect`, `onClose`, all optional.

### Removed

- **Breaking:** `set_celery_dispatch_hook` and `set_celery_kwargs_hook`. `Middleware.execute()`
  runs where the action executes, and the call's context reaches the worker for every action.

### Changed

- **Breaking:** a `celery_viewset` task carries the call's context as `_context`, no longer as the
  `context` kwarg. A worker and a FastAPI process of different versions do not understand each
  other's tasks.
- **Breaking (muxws):** `process_command` runs the request as a task of its own. When the stream
  ends before the answer (the peer cancels it or the socket is lost), the request runs to completion
  and its answer is discarded; cancelling the stream no longer cancels the request.
- **Breaking (npm):** a call that gets no answer is repeated, for up to 10 minutes by default, and a
  REST attempt times out after 30 s (`attemptTimeoutMs`); before, a REST call had no timeout of its
  own and no call was repeated. Without `Idempotency` on the backend a repeat runs the action again.
- **Breaking (npm):** `MuxwsProxyOptions.timeoutMs` bounds one attempt; an attempt that reaches it is
  repeated, up to `repeat.withinMs`, instead of rejecting the call with `StreamTimeout`.
- **Breaking:** the `muxws` extra requires `muxws>=0.4.0`, which requires `websockets>=14`.
- **Breaking (npm):** the optional peer dependency `muxws` is `^0.4.0`. A repeat over muxws carries
  the headers that the peer's open interceptors (`Peer.interceptOpen`, muxws 0.4.0) added to the
  first stream.
- **Breaking (npm):** `Idempotency-Key` is sent on every call; a cross-origin REST backend has to
  allow the header, and every call to it is preflighted.
- **Breaking (npm):** the actions of the mixin interfaces and of `ViewSetProxyBase` return
  `ViewSetActionCall<R>`, `request()` returns `ViewSetCall<R>`, and `RestProxy<M>` and
  `MuxwsProxy<M>` are `ViewSetCalls<M>`. A transport of one's own returns
  `issueCall(promise, key)` from `request()`.
- npm: `MuxwsProxyImpl.request()` opens its stream synchronously once the peer has resolved.
- npm: `ViewSetProxyBase`'s constructor binds every method of the instance's class chain as an own,
  non-enumerable property, which sets `key` on the method's promise.

## [0.8.0] - 2026-10-13

### Added

- `RateLimiter(key_prefix=...)`: the prefix of its Redis keys, default `"ratelimit:"`.
- `toErrorDescription(body)` converts a failed request's body into an `ErrorDescription`
  (`code`, `params`, `detail`, `origin: 'server'`), the shape of `ErrorDescription` in
  `@dynamicforms/vue-forms`: `detail_code` becomes `code` (`''` where the body has none),
  `detail_params` becomes `params`, and `detail` is kept.
- `toFieldErrors(body)` converts FastAPI's 422 validation body into `ErrorDescription`s keyed by
  field name (`loc` without its source element, joined with `.`), with pydantic's `type` as `code`,
  `ctx` as `params` and `msg` as `detail`.
- The `ErrorDescription`, `FieldErrorEntry` and `FieldErrorsBody` types.
- Every operation in a viewset's `/schema` (REST and muxws) carries `x-viewset-action`, the name of
  the viewset method that serves it.
- `restViewSet<T>()(pk, [])` and `muxwsViewSet<T>()(pk, [])` build a ViewSet with no standard
  action, for a BE viewset with custom endpoints only. An empty `declares` was a compile error
  (TS2507).
- `ReadOnlyCollectionError` (405, code `read_only`), raised by a write to a `CollectionViewSet`
  over an immutable container.
- The API reference has pages for `route_muxws` and `settings`, and lists the request, error-body,
  muxws, filter and Celery helpers.
- `celery_viewset` and `celery_viewset_client` take `result_timeout` (seconds, default 3600; `None`
  waits without a limit). A call whose result does not arrive in time raises `ResultTimeoutError`
  (504, `result_timeout`, params `task` and `timeout`).
- `get_process_queue_key(queue_key)`: the Redis list the calling process receives a queue key's
  results on.
- `RequestOptions.headers`: headers for a single `request()` call, sent by both transports; over
  muxws they override the proxy's `headers`.
- `ListShapeMismatchError` and the `ListShape` type.
- `ResultReaderStoppedError` (503, `result_reader_stopped`, param `queue_key`): a `celery_viewset`
  call pending when the result reader of its `queue_key` stops.

### Changed

- **Breaking (muxws):** a command goes through the application that serves the WebSocket endpoint
  (`connection.scope["app"]`, or `app=`), as an HTTP request to the same path does: its middleware,
  exception handlers (`df_viewset_exception_handler` included), router dependencies, `app.state`,
  lifespan state and `dependency_overrides` apply to both transports. The library no longer builds
  an app of its own for muxws; `get_dispatch_app` is removed. `process_command` without
  `connection` or `app` raises `ValueError`.
- **Breaking (muxws):** a command's `:path` is the path the application routes, `include_router`
  prefixes included, so a client uses one `basePath` for both transports.
- **Breaking (muxws):** each viewset endpoint is one route on the router given to `route_viewset`,
  matching only the transports it is published on (`fastapi_viewsets.routing.TransportRoute`); the
  request's transport is `scope["fastapi_viewsets.transport"]`. The router also holds the routes
  published on muxws only; those are left out of the application's OpenAPI document.
- A muxws command does not inherit the handshake's `connection`, `upgrade`, `sec-websocket-*` and
  `accept-encoding` headers, so a compression middleware leaves the reply uncompressed.
- An exception the application raises after it has started its response is logged, and over muxws
  the reply is that response (the application's own 500 handler included), as an HTTP client
  receives it.
- **Breaking:** requires Pydantic 2.11 or later.
- The body of `PATCH {pk}` and `PATCH bulk` is a partial model: every field optional, the primary
  key left out, a stated field validated by its annotation, its constraints and the model's
  `@field_validator`s. `@model_validator`s are not applied to it. `perform_update` and
  `perform_bulk_update` receive the model holding the stated fields only, so
  `data.model_dump(exclude_unset=True)` is what the request stated; on a `celery_viewset` the worker
  receives the same.
- **Breaking (Celery):** the JWT user crosses to a worker as the token and the position of its
  `JWTAuthBackend` in `settings.viewsets_auth_processors`, not the secret. The worker verifies the
  token with its own configured backend, so it needs the same `viewsets_auth_processors`.
- A filter value that will not convert (`?year__in=1999,abc`) is a 422 with `loc`
  `["query", parameter, index]`, on a `celery_viewset` as well.
- `limit` below 1 and `offset` below 0 are a 422.
- A muxws command whose `:method`, `:path` or `:query` is malformed is answered with a 400.
- `timeoutMs` of a muxws ViewSet bounds the wait for the reply headers as well as the body.
- `route_viewset` raises `ValueError` for an unknown `lifecycle`.
- **npm:** `vue` and `lodash-es` are no longer peer dependencies; the package imports neither.
- **Breaking (Django):** a relation field (`ForeignKey`, `OneToOneField`) appears in a
  `DjangoORMViewSet`'s `schema` under its name and carries the related row's key (`owner: int`). The
  backend reads, writes, filters and sorts it on the `owner_id` column. A `schema` declaring
  `owner_id` raises `TypeError` when the viewset class is defined, and `pk_field_name` for a
  one-to-one primary key is the relation's name (`artist`).

### Fixed

- `GET {base_path}/schema` over REST lists only the endpoints published on REST; it listed the
  endpoints published on muxws only as well.
- muxws commands carry the WebSocket connection's client and server address (`request.client`)
  when `process_command` is given `connection=`. Every command had the client `("muxws", 0)`, so
  `RateLimiter`'s default key counted all muxws callers of a viewset as one.
- `RateLimiter` with `redis_client` increments and sets the window's expiry in one Lua script and
  sets the expiry on any key that has none. A call interrupted between `INCR` and `EXPIRE` left a
  key that never expired, and a `window_seconds` below 1 set an expiry of 0, which deleted the
  key. The expiry is in milliseconds. Keys are prefixed with `key_prefix` (default `"ratelimit:"`).
- `RateLimiter`'s in-memory storage removes entries whose window has passed; it kept one entry for
  every identity key ever seen.

- `list()`, `listPage()` and `listCursor()` send `X-List-Shape` with the shape they return, so each
  receives its own envelope from a viewset with `list_shapes`. A reply in another shape throws
  `ListShapeMismatchError`; it was returned as the method's type with the envelope fields
  `undefined`.
- `DjangoORMViewSet` updates run in one transaction that reads each row with `SELECT ... FOR
  UPDATE`, sets the stated fields on it, validates it as `schema` (`@model_validator`s included)
  and saves it. Two concurrent updates of different fields of a row kept only the later one's
  fields, and a partial update was not checked by `@model_validator`s against the stored fields;
  an invalid row answers 422 with `loc` `["body", field]` (`["body", pk, field]` in bulk). A bulk
  update with a missing or invalid row writes none of its rows. A row `get_queryset` excludes is a
  404 for an update as for a retrieve.
- `DjangoORMViewSet` create and update write relation fields; a create writes a natural or
  relation primary key from the body and leaves out only a key the database or Django assigns.
- `celery_viewset` encodes an action's result like REST and muxws do: a dict is encoded
  recursively, and a value that is not JSON-native (`datetime`, `Decimal`, `UUID`, ...) takes the
  form FastAPI's response serialization gives it. A result such as `{"record": Item(...)}` failed
  with `TypeError: Object of type Item is not JSON serializable`. A `SerializableObject` nested in
  a dict or list, including in a `ViewSetResult` body, is reconstructed in the FastAPI process.
- `celery_viewset` with more than one FastAPI process sharing a Redis (several workers or replicas):
  each process receives its results on its own list, `{queue_key}:{host name}:{process id}`. All
  processes read one list per prefix, and a process that popped another process's result discarded
  it, leaving that request waiting indefinitely. The worker renews a result list's expiry to one
  hour on every push, so the list of an exited process is removed.
- `celery_viewset` client: a call cancelled while it awaits its result removes its pending entry,
  which stayed in the registry. `send_task` runs in a worker thread instead of blocking the event
  loop while it publishes to the broker.
- The body of a create with `pk_field_name` set is validated as the model declares it: `Field`
  constraints, aliases, `model_config` (e.g. `extra="forbid"`) and the model's validators apply, and
  the OpenAPI schema carries them. A body breaking them was accepted and passed to `perform_create`
  unvalidated. A primary key the body gives as anything but null answers 422 with
  `pk_assigned_by_server`; it was ignored. `perform_create` receives the model with its fields as
  validated, without the primary key unless the model gives it a default, and `model_fields_set`
  names the fields the body stated.
- `celery_viewset` worker: a model argument is validated as its model, reading fields by name, where
  a value failing validation was passed on unvalidated through `model_construct` with nested models
  as dicts; a model with an aliased field always took that path. A partial update stating an
  aliased field failed on the worker. A create body without its primary key is validated as on the
  FastAPI side.

- The schema check matches a custom endpoint to a ViewSet method by the BE method's name
  (`x-viewset-action`), in camelCase or as it is, where it read the first path segment: an endpoint
  under a path parameter (`POST /items/{pk}/approve`) asked for a `{pk}()` method and one with a
  hyphen (`GET /items/top-rated`) for a `top-rated()` method, and both were reported as missing. An
  operation without `x-viewset-action` is not checked.
- A route whose endpoint declares no `context` parameter builds its context for the command
  middleware chain; with `Authorization` (or any middleware reading the context) configured it
  answered 500.
- `PATCH` accepts a body that leaves fields out; it answered 422 for any field missing.
- A custom endpoint that declares `request: Request` receives the request; the call raised
  `TypeError` (`got multiple values for keyword argument 'request'`).
- `CollectionViewSet`: a partial update of a pydantic model item answered 500; a delete from a set
  never found the item; a partial update of a set's item replaced it with the request body.
- `SerializeStateSlots.deserialize_state` while nothing is stored (None) raised.
- The demo shows a failed call's server `detail` and `code` through `toErrorDescription`.

### Removed

- **Breaking (npm):** `translateApiError`, `translateStrings` and `translatableStrings`, and the
  `@dynamicforms/translatable` peer dependency. The package holds no translations; the application
  translates an error by its `code`.

## [0.7.3] - 2026-10-04

### Removed

- `@dynamicforms/vue-forms` is no longer a peer dependency of the npm package, which imports nothing
  from it.

## [0.7.2] - 2026-09-25

### Changed

- The npm package is ESM only: the UMD build is dropped. `exports["."]` points at the ES module and
  states a `types` condition first, so TypeScript with `bundler` or `node16` module resolution finds
  `dist/index.d.ts`.

### Removed

- The `./styles.css` export, which named a file the package does not ship.

## [0.7.1] - 2026-09-24

### Added

- A registered route endpoint can return a `ViewSetResult` directly instead of a plain body, to
  reach `status_code`/`headers`/`cookies` itself without writing a command middleware - a
  `perform_*` hook cannot, since it isn't itself a registered route.
- `ViewSetResult` is now generic (`ViewSetResult[T]`) - an endpoint declared `-> ViewSetResult[X]`
  documents/validates exactly like a plain `-> X`; `route_viewset`/`build_schema` unwrap it to `X`
  for the OpenAPI `response_model`.
- `ViewSetResult` is now a `SerializableObject`, so a `celery_viewset`-dispatched action that
  returns one directly survives the worker boundary the same way any other `SerializableObject`
  does.
- `fastapi_viewsets.context.serialize_value`/`deserialize_value` - the tagged-payload convention
  `serialize_context`/`deserialize_context` already use per context key, now available for a single
  `SerializableObject` value outside a context dict.
- `fastapi_viewsets.context.to_jsonable` - the shared JSON-safe conversion (Pydantic model dumped,
  list/tuple recursed into, `SerializableObject` tagged) `ViewSetResult`'s own serialization and
  `celery_viewset`'s worker/client both use, replacing three near-duplicate copies of the same
  logic. `celery_viewset_client`'s call-argument serialization now also tags a bare
  `SerializableObject` argument (not just one wrapped in a `Context`), which it previously did not.

## [0.7.0] - 2026-09-13

### Added

- `Middleware.modifies_response_shape` (also settable as a plain attribute on a function-based
  middleware) lets a command middleware declare whether it changes the shape of
  `ViewSetResult.body` - defaults to `False`.

### Fixed

- Command middleware no longer disables a route's `response_model` for the whole application just
  because *any* middleware is configured in `settings.viewsets_command_middleware`. Previously, even
  a middleware that only attaches a cookie or header (never touching `ViewSetResult.body`) caused
  every route's OpenAPI response schema to go untyped and FastAPI's own response validation to be
  skipped. `response_model` is now only disabled when a configured middleware declares
  `modifies_response_shape=True`.

## [0.6.3] - 2026-09-03

### Fixed

- `celery_viewset_client` now sends its Celery task with `ignore_result=True` explicitly.
  `celery_viewset_server` already registers every task with `ignore_result=True`, but Celery's
  `send_task()` defaults that setting to `False` per call, and a request-level value overrides the
  task's own - so every task's return value was still handed to Celery's own result backend, which
  raises a Kombu `EncodeError` for any non-JSON-safe value such as a raw Pydantic model.
- `celery_viewset_server`'s task no longer returns the endpoint's raw result to Celery once that
  result has been pushed to the Redis queue - `celery_viewset_client` never reads a task's own
  Celery retval, only that queue entry, so handing the raw value (e.g. a Pydantic model) to Celery
  a second time served no purpose and is exactly what `ignore_result=True` above should, but isn't
  guaranteed everywhere (e.g. worker task-succeeded events) to prevent Celery from trying to encode.
- `celery_viewset_server`'s kwarg reconstruction now recurses into `list[Model]` and
  `tuple[Model, ...]` (including their `Optional` form), not just a bare `Model` hint. A parameter
  typed as a list of Pydantic models previously arrived at the endpoint as a list of plain `dict`s.

## [0.6.2] - 2026-09-03

### Fixed

- `celery_viewset_client` no longer raises a Kombu `EncodeError` when an action argument is a
  `list[BaseModel]` or `dict[str, BaseModel]`. Serialization now recurses into `list`, `tuple`,
  and `dict` values so nested `BaseModel` instances are converted at any depth before being
  handed to `send_task()`.

## [0.6.1] - 2026-09-03

### Fixed

- `typecast_to_original_model` no longer raises a 500 for a body parameter annotated as
  `Model | None` (or `Optional[Model]`): the union was previously passed straight into
  `isinstance()` and `model_validate()`/`model_construct()`, none of which a union object
  supports. It is now unwrapped to its `BaseModel` member first.

## [0.6.0] - 2026-09-01

### Added

- `fastapi_viewsets.exceptions`: every `HTTPException` this package raises on its own behalf -
  `NotFoundError`, `SessionExpiredError`, `NotAuthorizedError`, `RateLimitedError`,
  `UnsupportedListShapeError`, `CursorRequestError` - now carries a stable `code` and the `params`
  its English `detail` was interpolated with, as attributes on the exception itself. `detail`
  stays a plain string, exactly as it always was - nothing changes in the response body by
  default.
- `df_viewset_exception_handler`, an opt-in exception handler for `DfViewSetError` (the common
  base every class above descends from). Registered once -
  `app.add_exception_handler(DfViewSetError, df_viewset_exception_handler)` - it adds
  `detail_code` and `detail_params` alongside `detail` in the response body, so a frontend can
  translate or switch on `detail_code` without parsing English prose out of `detail`. An
  application that does not register it sees exactly what it always has.
- The Vue client gains `translateApiError()`, which looks up `detail_code` in a
  `@dynamicforms/translatable` table of English defaults and interpolates `detail_params` into
  it - returning `detail` unchanged when `detail_code` is absent (the handler above was not
  registered, or the error is a view's own plain-string `HTTPException`) or names a code the
  table does not cover. Call `translateStrings()` (also exported) to supply translations for as
  many codes as the application has.
- A new docs page, [Error codes](https://docs.velis.si/dynamicforms/fastapi-viewsets/guide/error-codes),
  covers the design and the frontend translation recipe in full.

### Changed

- Bumps the `@dynamicforms/vue-forms` peer range to `^1.0.0` and adds `@dynamicforms/translatable`
  (`^0.1.0`) as a peer dependency.
- `NotFoundError` moves from `fastapi_viewsets.response_classes` to `fastapi_viewsets.exceptions`,
  alongside the other exception classes above.

## [0.5.7] - 2026-08-26

### Fixed

- `muxws` is no longer imported at module load time. `fastapi_viewsets.decorators` (and therefore
  the whole package) now imports cleanly without the `muxws` extra installed; code that actually
  builds a muxws response payload still requires the package and raises `ModuleNotFoundError` if
  it's missing.
- `Authorization` no longer puts a callable `@action_configuration` value into
  `context.authorization`. A callable config is a `depends()`-only gate and was never meant to
  reach `perform_*` or cross into a `celery_viewset` worker; doing so unconditionally made every
  request to a callable-gated, celery-served viewset fail with `TypeError: Object of type function
  is not JSON serializable` once `context` was serialized for the worker.

## [0.5.5] - 2026-08-24

### Changed

- `ViewSetProxyBase.pkFieldName` is now public instead of protected, so a caller holding a
  ViewSet instance without knowing its concrete class — a grid or table component asking which
  field identifies a row — can read it. A factory-built class's constructed instance type now
  includes `pkFieldName`, typed as the literal field name (e.g. `'id'`) rather than widened to
  `string`.

## [0.5.4] - 2026-08-23

### Changed

- `set_celery_dispatch_hook` now receives the call's current kwargs (a `Context`, if the action
  declares one, included), instead of taking no arguments. This lets a registered hook read what a
  context processor already put there instead of needing its own request-level plumbing.

## [0.5.3] - 2026-08-23

### Changed

- `route_rest` and `route_muxws` now fail to compile when passed a factory-built ViewSet class,
  instead of silently handing back an object missing that class's own methods. The error surfaces
  as `TS2339` on the first property or method accessed on the result, not on the `route_rest` /
  `route_muxws` call itself.
- Restating `static declares` on a subclass of a factory-built class (already a compile error)
  reports a shorter, clearer `TS2417` that stops at `declares` itself instead of drilling into
  which mixin method is missing.
- `STANDARD_FE_METHODS` in `proxy-base.ts` is now derived from a `Record<ActionName, true>`
  object literal, so an action added to (or removed from) `ActionName` without a matching update
  fails to compile instead of silently narrowing what the FE/BE schema mismatch check can report.

### Fixed

- `vite.config.ts` no longer uses `__dirname`, which `vite`'s native config loader warns is
  unsupported and slated to become the default loader in a future major version.

## [0.5.2] - 2026-08-23

### Added

- `celery_viewset_server` and `celery_viewset_client` each expose a hook slot for external
  packages to attach dispatch/execute logic without this repo depending on them.
  `set_celery_kwargs_hook` registers a `Callable[[Callable, dict], tuple[Callable, dict]]` run on
  the worker side just before `_reconstruct_kwargs`, receiving the run-callable and the raw kwargs
  and returning both, possibly changed. `set_celery_dispatch_hook` registers a
  `Callable[[], Awaitable[dict]]` awaited on the client side just before `send_task`, its returned
  dict merged into the task kwargs. Both default to `None`, leaving current behavior unchanged.

## [0.5.1] - 2026-08-22

### Changed

- `vuetify-inputs` in the demo moves to `^0.9.1` and `vue-grid` to `^0.3.1`. Dev dependencies
  `@types/node` and `eslint-config-velis` move to `^26` and `^3`.

## [0.5.0] - 2026-08-22

### Changed

- The `@dynamicforms/vue-forms` peer dependency moves to `^0.17.1`, `vue-grid` in the demo to
  `^0.3.0`, and `vuetify-inputs` to `^0.9.2`. Build, lint and the Vue test suite pass unchanged
  against the new versions.

## [0.4.0] - 2026-08-16

### Added

- **muxws transport.** A viewset registered with `route_viewset` is now also reachable over a
  [muxws](https://docs.velis.si/muxws) WebSocket alongside REST. A command is dispatched into a
  FastAPI app the library builds from the endpoints published on muxws, each carrying the same
  route kwargs `route_viewset` built for the REST router — so validation, dependencies, response
  models and `settings.viewsets_command_middleware` behave identically on both transports. That
  dispatch app is the library's own, not the application object you created: middleware installed
  on your app, exception handlers beyond the framework's defaults for `HTTPException` and
  `RequestValidationError`, `app.state` and `app.dependency_overrides` are not reached by a command
  unless you pass `app=` to `process_command`. Registration is resolved at three levels, each free
  to defer to the next: `@transports(...)` per endpoint, `register_rest`/`register_muxws` on
  `route_viewset`, and `settings.viewsets_register_muxws` (default `True`). The response status
  arrives on the first data frame a peer sends, read by awaiting `replyHeadersArrived` before
  consuming the body, so a caller does not have to read a whole streaming response before learning
  it failed. `fastapi_viewsets.mux_ws` exports `process_command`, `transports`, `register_viewset`
  and the resolvers behind the three registration levels; the registry warns when two viewset
  classes register the same `base_path`, since the later one's endpoints become unreachable.
  Requires the new `muxws` extra (`pip install "dynamicforms-fastapi-viewsets[muxws]"`, npm peer
  `muxws@^0.3.1`, needed for response headers on data frames).
- **Vue client support for muxws.** `route_muxws`, alongside `route_rest`, builds a `MuxwsProxyImpl`
  that sends the same ViewSet calls over a muxws stream — the request line as pseudo-headers, the
  body as payload, the status read off the reply headers. The transport-independent parts of the
  proxy (bulk operations, lookup, request building) are factored into a shared `ViewSetProxyBase`,
  so `RestProxyImpl` and `MuxwsProxyImpl` implement every ViewSet method identically and a proxy
  instance speaks exactly one transport. `ViewSetRequestError`, thrown on a failed call over muxws
  (there is no axios to raise anything there), mirrors `AxiosError`'s shape —
  `error.response.status`, `.data`, `.headers` — so error-handling code written against one
  transport keeps working against the other; unlike `AxiosError.response`, it is always set. A
  custom endpoint method should call the shared `this.request()` rather than `this.http`, so the
  same body works on either transport (`this.http` is unchanged on `RestProxyImpl`).
- **A lazy list pipeline.** `perform_list` may return any iterable or async iterable instead of a
  materialised list, so a source that cannot be cheaply enumerated no longer has to be built in
  memory up front. Filtering, sorting and paging are `apply_filter`/`apply_sort`/`apply_pagination`
  stages a subclass overrides and chains via `super()`; a stage that answers part of the query
  itself calls `query.mark_applied(...)`, and only what nothing marks applied still runs in memory:

  ```python
  async def apply_sort(self, context, query: ListQuery, records: ListRecords) -> ListRecords:
      if query.has_sort:
          records = self.db.order_by(*(c.column_name for c in query.sort))
          query.mark_applied("sort")
      return await super().apply_sort(context, query, records)
  ```

  `ListRecords`, `materialize()` and `take_page()` are generic in the item type
  (`ListRecords[TItem]`), so a stage's return type states what its items are instead of erasing them
  to `Any`; `perform_list` itself stays annotated bare (`ListRecords[Any]`), since what a backend
  yields is its own row type and records are `T` only once `to_record()` has run over them.
- **Three list response shapes.** A viewset declares `list_shape` (`"plain"`, `"paginated"` or
  `"cursor"`, defaulting to `settings.default_list_shape`, itself `"plain"`) and, optionally,
  `list_shapes` — the shapes a client may additionally request per call with an `X-List-Shape`
  header. `route_viewset` builds the endpoint's parameters and response model from these: query
  parameters no allowed shape uses are dropped, the header is kept only when there is a genuine
  choice, and the response narrows to a union of exactly the models the viewset can produce — one
  model, no header, when only one shape is declared. `PaginatedListMixin` and `CursorListMixin` are
  one-line shorthands for `list_shape = "paginated"` / `"cursor"`. A plain-array response is now
  `ListOf[T]`, a named `RootModel`, so its OpenAPI schema gets a real component name instead of an
  auto-generated one; the JSON payload is unchanged, still a bare array. `ListOf` also synthesizes a
  one-row OpenAPI example from the item model's schema, and a viewset declaring more than one shape
  gets one named example per shape, keyed by the `X-List-Shape` value that produces it.
- **Offset pagination.** `PaginatedListMixin` answers `{results, offset, limit, count, has_more,
  has_previous}`; pagination is opt-in per request, so an endpoint without `limit` still answers a
  plain list. Paging a lazy source reads only `limit + 1` rows regardless of source size, and
  `count` is `null` where the source cannot be counted without draining it — a backend that can
  count cheaply overrides `count_records()`.
- **Cursor pagination.** `CursorListMixin` pages by position: reaching a distant page does not
  re-read the rows in front of it, and a row inserted or deleted behind the client cannot make the
  next page repeat or skip a row. The cursor carries the full ordering-key tuple with the primary
  key appended, travels as real JSON values, and is coerced back through the response model's field
  types on read; it is fingerprinted against the ordering and the active filters, and a stale or
  mismatched cursor is a 400. The envelope exposes `next`/`previous` (the page's edges, read
  exclusively) and `first`/`last` (the same two edges, read inclusively, present whenever the page
  is non-empty) rather than anchoring outside the page, so the anchors survive concurrent inserts;
  there is no total, since producing one would require draining the source. NULL placement follows
  `ListMixin.nulls` (`"first"`, the default, or `"last"`) under an ascending sort — descending
  reverses it — and the in-memory sort, the cursor's row comparison and a backend's own SQL agree on
  the same rule, which is what keeps a cursor walk from skipping NULL rows. The cursor predicate is
  itself a filter and reaches a backend through the same compilation registry as any other one.
  `CursorListMixin`/`listCursor()` is mirrored on the Vue client.
- **Declarative filters.** A filter is data — a registered operator name plus a value — evaluated in
  memory by a single `matches()` implementation that is always available.
  `fastapi_viewsets.filters.make_filter_model(model, declaration)` builds a filter model from a
  field/operator declaration, and `@compiles(backend, filter_type)` lets a backend register its own
  translation of a given filter, keyed on `(backend, filter)`: a new operator needs no backend
  release, a new backend needs no filter changes. Built-in operators: `exact`, `iexact`, `contains`,
  `icontains`, `startswith`, `gt`, `gte`, `lt`, `lte`, `in`, `isnull`, `overlaps`. Push-down is
  all-or-nothing per filter — a backend either translates it fully into its own query or declines
  and lets the in-memory fallback run. `in` and `overlaps` take a comma-separated string rather than
  a repeated query parameter, matching `sort`'s existing convention, since FastAPI silently drops a
  list-typed field from a `Depends()`-expanded model.
- **`DjangoORMViewSet`**, a second concrete `ImplMixin` backend, in
  `fastapi_viewsets.backends.django_orm`. `perform_list` returns the queryset itself, unevaluated;
  `apply_filter` translates the
  filter model into `.filter(**criteria)` for every field that maps onto a concrete model field or
  its attribute name, and declines otherwise; sort pushes both directions down, including the
  correct `nulls_first`/`nulls_last` placement, rather than only ascending. `to_record()`, an
  overridable hook converting a Django row to the response model, runs on the final page alone for
  as long as no stage has landed the source — a declined filter or sort still runs it over whatever
  reached that stage, which can be the whole table. `pk_field_name` is derived from the model's
  primary key rather than defaulting to the literal `"id"`; a composite primary key raises
  `TypeError` naming the class and asking for `pk_field_name` to be set explicitly. Requires the
  existing `django` extra.
- **`@endpoint_docs({...})`** attaches a per-viewset summary, description, response_description,
  `deprecated` flag and tags to an individual mixin-provided endpoint (`list_items`, `create`,
  `retrieve`, …), since those endpoints' own docstrings are shared library code and otherwise
  describe every viewset identically. It must sit below `@route_viewset` on the class — decorators
  apply bottom-up — and raises `ValueError` if placed above, or if it names an action the viewset
  does not have. A viewset's own docstring, where it declares one rather than inheriting a mixin's,
  becomes the OpenAPI tag description for its endpoint group; `apply_viewset_tags(app, extra=...)`
  applies the collected descriptions to a `FastAPI` app, needed because `get_openapi()` does not read
  `app.openapi_tags` once an application has replaced `app.openapi`.
- **Vue: mixin classes carry their action names.** `CreateMixin`, `ListMixin`, `BulkViewSetMixin` and
  the rest export a `static actions: readonly string[]` naming the frontend method(s) they
  contribute, as real values rather than types, so a `ViewSet` can list them in a
  `static declares = [Mixin, ...]` array read at runtime. `restViewSet<T>()(pkFieldName,
  [Mixin, ...])` and `muxwsViewSet<T>()(...)` return a class to extend whose methods are both
  narrowed to the declared actions and actually callable — unlike `route_rest`'s narrowed type,
  which could name a method the bare-proxy object it returned did not have:

  ```ts
  import { restViewSet, CursorListMixin } from '@dynamicforms/fastapi-viewsets';

  class TrackViewSet extends restViewSet<Track>()('id', [CursorListMixin]) {}
  const tracks = new TrackViewSet({ basePath: '/tracks' });
  const first = await tracks.listCursor({ limit: 50 });
  ```

  `route_rest` and `route_muxws` remain supported unchanged. A `ViewSet` that declares nothing is not
  schema-checked and does not pay for the schema request.

### Changed

- **Breaking:** `ListMixin.setup_filter`/`setup_sort`, the pre-filter/pre-sort hooks that mutated
  instance state ahead of `perform_list`, are removed. Override `apply_filter`/`apply_sort` instead
  — both now receive the query and the records together, so nothing needs to be stashed on the
  instance beforehand.
- Vue: the internal endpoint-to-method table used by the startup schema check maps each REST
  path/method pair to the set of frontend action names that can satisfy it (`list`, `listPage` and
  `listCursor` all answer `GET {base}`, since the three list shapes are one backend endpoint) rather
  than a single fixed name.

### Fixed

- `RestProxyImpl`'s startup schema check no longer warns about a backend endpoint being "missing"
  whenever a ViewSet is built from one mixin, or none. `typeof this[action] === 'function'` is true
  for every action on every proxy regardless of what the ViewSet actually declares, so the check
  always restated the schema it had just fetched; it is now driven by the ViewSet's own `static
  declares` list. It also now checks `listPage`/`listCursor` in both directions and no longer
  misclassifies a nested path under `/{pk}` as `retrieve`.
- `route_rest`/`route_muxws` were dropping the ViewSet class parameter, so no declaration reached the
  proxy for the schema check to read; the declaration is now carried across.
- TypeVar resolution recognizes a parameterised pydantic generic. Pydantic's `M[X]` is a class, not a
  typing alias, so `get_origin` did not see it and `PaginatedList[T]` reached the OpenAPI schema
  unresolved.

### Documentation

- muxws is documented as dispatching into the library's own app rather than your application object
  — see the Added entry above for exactly what that leaves unreached, and `app=` on
  `process_command` for reaching your application anyway.
- `route_viewset`'s reference page documents `register_rest` alongside `register_muxws`;
  `ViewSetRequestError` is named on both the Python and Vue references, and its docstring no longer
  claims both transports throw it.
- NULL placement is documented as first under an ascending sort by default, not last; the Python
  reference gains `PaginatedListMixin`, `CursorListMixin`, and everything this release adds to
  `ListMixin` — the class attributes, the `offset`/`limit`/`cursor`/`X-List-Shape` parameters and the
  pipeline hooks.
- A backend-authoring guide states the contract a `@compiles` implementation follows:
  `filter_set_for`, `filters_from`, `can_compile_all`/`compile_all`, `mark_applied`/`needs`, and
  `land()` as the call that ends the lazy part.
- New guide pages: [muxws transport](https://docs.velis.si/dynamicforms/fastapi-viewsets/guide/muxws)
  and [The list pipeline](https://docs.velis.si/dynamicforms/fastapi-viewsets/guide/list-pipeline)
  (filters, offset pagination, cursor pagination).

## [0.3.6] - 2026-08-01

### Fixed

- A `BaseModel` field holding a `date` or `datetime` no longer breaks a `celery_viewset` call. Both
  the client, serializing an argument before `send_task()`, and the server, serializing a result
  before pushing it to Redis, call `model_dump(mode="json")` rather than `model_dump()`, so a
  `date`/`datetime` value becomes a JSON string instead of an object `json.dumps()` — what Kombu's
  transport uses — cannot encode.
- A positional argument to a `celery_viewset_client`-wrapped method is serialized the same way a
  keyword argument already was. Only `kwargs` were passed through the serializer; a `BaseModel` or
  `Context` passed positionally reached `send_task()` unconverted.

## [0.3.5] - 2026-07-30

### Added

- `check_result_readers()` compares every `queue_key` registered by a `celery_viewset_client`
  decorator against the ones with a running result reader and logs a warning for each mismatch.
  Call it once, right after starting readers in the FastAPI `lifespan`, so a missing reader shows up
  as a log line at startup instead of a silent hang on the first request.
  `get_registered_queue_keys()` and `get_running_queue_keys()` are exported alongside it from
  `fastapi_viewsets.decorators.celery_viewset`.
- A [Django Integration](https://docs.velis.si/dynamicforms/fastapi-viewsets/guide/django-integration)
  guide page: initializing Django in both the FastAPI and Celery worker processes,
  `autodiscover_tasks` for a `celery_viewset`-decorated class outside a conventional `tasks.py`, and
  the `sync_to_async` convention for Django ORM access from `perform_*`.

### Changed

- `start_result_reader(redis_client, queue_key)` and `stop_result_reader(queue_key=None)` take a
  `queue_key` and keep one reader task per key instead of a single global one, so an app with more
  than one `celery_viewset` prefix runs a reader for each. `stop_result_reader()` with no argument
  stops every running reader; passed a `queue_key` it stops only that one.

### Documentation

- [Combining both decorators](https://docs.velis.si/dynamicforms/fastapi-viewsets/guide/routers#combining-both-decorators)
  states the two valid ways to apply `celery_viewset` and `route_viewset` to the same viewset —
  subclassing in `main.py` versus stacking both decorators on one class — and the order stacking
  requires: `celery_viewset` inner, `route_viewset` outer. The reverse order leaves `celery_viewset`
  iterating `route_viewset`'s FastAPI-wrapped routes instead of the raw viewset methods, registering
  a bogus `schema` Celery task.

## [0.3.2] - 2026-07-13

### Changed

- **Breaking:** the context serializer's type-tagging keys are `__fpv_type__` and `__fpv_value__`,
  replacing `__type__`/`__value__`. The prefix avoids colliding with Kombu's own `__type__`/
  `__value__` convention for its JSON codec — without it, a Celery worker's `object_hook` intercepted
  a `SerializableObject` payload before it reached `deserialize_context()`.

## [0.1.0] - 2026-07-13

### Added

- Initial implementation: Python mixin classes (`CreateMixin`, `ListMixin`, `RetrieveMixin`,
  `UpdateMixin`, `DestroyMixin`, and bulk counterparts) that compose into FastAPI CRUD/bulk
  endpoints; the `route_viewset` decorator, registering a mixin-composed ViewSet on a FastAPI
  `APIRouter` in one call and building its OpenAPI schema; `CollectionViewSet`, a zero-boilerplate
  in-memory ViewSet backed by a list, set or dict; a `celery_viewset` decorator moving a ViewSet's
  execution to a Celery worker — task dispatch plus a Redis-backed result reader — with no changes
  to the ViewSet body; three instance lifecycle modes (singleton, per-request, instance-key) with
  optional `load_state`/`save_state` hooks; a Vue/TypeScript client counterpart (`route_rest` and
  `RestProxyImpl`) mirroring the mixin method names against a typed Axios client; a documentation
  site and a runnable demo app.
- `RestProxyImpl` validates its declared methods against the backend's OpenAPI schema at
  construction: it fetches `<basePath>/schema` and cross-checks the standard CRUD/bulk/lookup paths
  against the frontend method set, logging a `console.warn` for a frontend method with no matching
  backend endpoint, a backend endpoint with no frontend method, or a non-standard backend endpoint.
  This costs one extra `GET` request per instantiation; a failure fetching the schema is swallowed
  and never affects normal proxy operation.
- **Context processors.** `settings.viewsets_context_processors` is a list of async callables run on
  every request to build a per-request `Context`, injected into any ViewSet method that declares a
  `context: Context` parameter. Field access on `Context` (`context.foo` / `context["foo"]`) is
  always awaitable, whether the underlying value is a plain eager one or a `LazyObject` — resolved on
  first `await`, memoized, sync or async. A `SerializableObject`/`LazyObject` value can define
  `__serialize__`/`__deserialize__`, or for `LazyObject` a recipe it can re-resolve from, so it
  survives the JSON round trip through Celery/Redis when the action it belongs to runs via
  `celery_viewset`.
- **Command middleware.** `settings.viewsets_command_middleware` is an ordered list of
  `CommandMiddleware` — a plain async function, or a `Middleware` subclass — forming an onion-style
  chain around every ViewSet action. Each layer receives `(request, viewset, context, call_next)` and
  returns a `ViewSetResult` (`body`, `headers`, `cookies`, `status_code`); once the chain finishes,
  `headers`/`cookies` are applied to the real `Response` and `status_code` overrides the response
  status. A `Middleware` subclass may additionally implement `depends()`, bridged onto FastAPI's own
  `Depends()` so it runs — and can reject with an `HTTPException` — before the request body is
  parsed. Middleware only runs for HTTP-routed requests; a Celery worker executing an action via
  `celery_viewset` has no live `Response` and skips the chain.
- **`@action_configuration({...})`** attaches per-viewset or per-method configuration, keyed by an
  identifier (a context-processor callable or a `Middleware` (sub)class), read at request time via
  `Context.configuration_for`/`Middleware.config_from`. Configuration merges in priority order:
  `settings.default_action_configuration` → class-level `@action_configuration` → method-level
  `@action_configuration`. A value can vary by action name via `ByAction(default=..., **by_action)`,
  or itself be a `Middleware` instance, injecting an extra middleware for just that call.
- **Authentication.** `AuthBackend` is an abstract base for recognizing credentials in a request;
  `settings.viewsets_auth_processors` is tried in order, and `auth_context_processor` wires the first
  backend that claims a request into `context.user` (or `None`). Three backends ship:
  `StaticUserAuthBackend`/`StaticUserCookieAuthBackend` (a fixed token-to-user mapping, via header or
  cookie), `DjangoSessionAuthBackend` (resolves a Django session key against
  `django.contrib.auth.get_user`, requiring the new `django` extra), and `JWTAuthBackend` (verifies
  a Bearer JWT's
  signature and expiry locally, requiring the new `jwt` extra). The `Session` command middleware
  rejects a request with 401 when `context.user` is `None`; a viewset or method opts out with
  `@action_configuration({Session: False})`.
- **Authorization.** The `Authorization` command middleware evaluates a configured check — a callable
  `check(request, cls, context)` rejects with 403 in `depends()` before the action runs, or a plain
  value is exposed as `context.authorization` for a `perform_*` method to inspect and reject on its
  own once it has the fetched record.
- **Rate limiting.** The `RateLimiter` command middleware enforces a fixed-window request count per
  identity key (default `"<ViewSetClassName>:<client IP>"`, overridable via `key_func`), backed by
  an in-memory dict or a shared `redis.asyncio.Redis` client for multi-process correctness; exceeding
  the limit rejects with 429 in `depends()`. The per-viewset/method limit is set via
  `@action_configuration({RateLimiter: <n>})`.
- `settings.viewsets_security_scheme` attaches a FastAPI security scheme as an extra dependency on
  every viewset route, so Swagger/OpenAPI shows an Authorize lock icon and a testable flow; it has no
  other effect on request handling.
- New optional install extras: `django` (`django`, `asgiref`) for `DjangoSessionAuthBackend`, and
  `jwt` (`PyJWT`) for `JWTAuthBackend`.
- Packaging for publishing: a full `hatchling` build configuration in `pyproject.toml`, `__version__`
  in `fastapi_viewsets/__init__.py`, a `py.typed` marker, and a `README.md`.

### Changed

- **Breaking:** every `perform_*` method (`perform_create`, `perform_list`, `perform_retrieve`,
  `perform_update`, `perform_bulk_create`, `perform_bulk_update`, `perform_destroy`,
  `perform_bulk_destroy`) takes `context: Context` as its first parameter, ahead of the existing ones —
  `perform_retrieve(self, context: Context, pk: K) -> T`. `CollectionViewSet`'s built-in
  implementations already do; a hand-written override adds the parameter.
- **Breaking:** the npm package is renamed from `@dynamicforms/viewsets` to
  `@dynamicforms/fastapi-viewsets`, and the GitHub repository from `dynamicforms/viewsets` to
  `dynamicforms/fastapi-viewsets`; imports change accordingly — `import { route_rest } from
  '@dynamicforms/fastapi-viewsets'`.
- Response-level side effects, such as setting a cookie, are produced by the command middleware chain
  rather than a per-viewset hook: a middleware sets `ViewSetResult.headers`/`cookies`/`status_code`,
  applied to the real `Response` once the chain completes. `route_viewset` always injects a
  `response` parameter, and disables automatic `response_model` inference for every route as soon as
  any command middleware is configured globally.
- `route_viewset` attaches a middleware-bridging `Depends()` to every registered route, so a
  `Middleware.depends()` hook — session, authorization, rate-limit — runs before FastAPI parses the
  request body.

### Fixed

- `route_sort_key`, used to order registered routes, no longer raises `TypeError: '<' not supported
  between instances of 'int' and 'tuple'` for a viewset with custom action paths of different depths
  sharing a common prefix (`account/register` alongside `account/register/verify/resend`). The path
  portion of the sort key is kept as a nested tuple instead of being flattened, so it compares
  consistently regardless of path depth.

### Removed

- The unused `CeleryViewSet` class (`fastapi_viewsets.celery_viewset`) and its documentation pages.
  It was never wired into `route_viewset`, and its `perform_*` methods returned raw,
  non-JSON-serializable Celery `AsyncResult` objects directly, so it could not serve as an HTTP
  viewset. The `celery_viewset` decorator is the supported way to back a viewset with Celery.
