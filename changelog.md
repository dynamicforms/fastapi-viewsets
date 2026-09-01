# Changelog

All notable changes to `fastapi-viewsets` — published as `dynamicforms-fastapi-viewsets` on PyPI
and `@dynamicforms/fastapi-viewsets` on npm — will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] - 2026-09-01

### Added

- Every `HTTPException` this package raises on its own - not-found, session-expired,
  not-authorized, rate-limited, an unsupported list shape, and every cursor-pagination error -
  puts a structured `{message, code, params}` object under `detail` instead of a plain string.
  `message` is the English default, fully interpolated; `code` is a stable identifier independent
  of `message`'s wording; `params` are the raw values `message` was interpolated with. A host
  application's own `raise HTTPException(status_code, detail="...")` is unaffected and still puts
  a plain string there.
- The Vue client gains `ApiErrorDetail`, `isApiErrorDetail()` to narrow an error's `detail` before
  reading `code`/`params` off it, and `translateApiError()`, which looks up `code` in a
  `@dynamicforms/translatable` table of English defaults and interpolates `params` into it -
  falling back to the server's own `message` for a code the table does not cover. Call
  `translateStrings()` (also exported) to supply translations for as many codes as the
  application has.

### Changed

- **Breaking:** Every error response's `detail` is now the object described above rather than a
  plain string, for the built-in errors listed there. A consumer reading `error.response.data.detail`
  as a string needs to read `.message` (or `.code`) off it instead - see the
  [migration guide](https://docs.velis.si/dynamicforms/fastapi-viewsets/guide/migration).
- Bumps the `@dynamicforms/vue-forms` peer range to `^1.0.0` and adds `@dynamicforms/translatable`
  (`^0.1.0`) as a peer dependency.

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
- `vuetify-inputs` in the demo moves to `^0.10.0` and `vue-grid` to `^0.3.1`. Dev dependencies
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
