# GAPS

Decisions taken without being able to ask, and the reasoning behind them. Each is a thing to
argue about, not a thing that is settled.

## muxws transport

### Dispatch goes through a synthetic ASGI request

`process_command` builds an ASGI scope and calls the FastAPI app, rather than reaching into
`lifecycle_runner` directly. The upside is total parity: parameter binding, validation,
`Depends`, command middleware, context processors, response models and status codes are the
same code on both transports, so they cannot drift.

The cost is one JSON decode/re-encode round trip per call. muxws has already decoded the payload
by the time we see it; we re-encode it to bytes for FastAPI to parse, and decode FastAPI's
response bytes again to hand back to muxws. Direct dispatch would avoid this, at the price of
re-implementing parameter binding - which is the duplication the transport exists to avoid.

If profiling ever shows this matters, the honest fix is not to bypass FastAPI but to let it
accept a pre-decoded body. Worth measuring before doing anything.

### Response status travels in leading headers

Resolved. It rode in `trailers` until muxws 0.3.1, which carries `headers` on the first `data`
frame a peer sends as well as on `open` (WSM-FRM-016). Trailers only attach to a frame with
`end: true`, so the status arrived *after* the body - free for a unary reply, wrong for a
streaming one, where a caller would have had to read a whole response to discover it was an error.

The client now awaits `replyHeadersArrived` before reading the body, so the status is known first.
Minimum muxws is 0.3.1 on both packages.

### An unhandled exception answers 500 rather than resetting the stream

muxws' own idiom is that a handler which raises produces `reset(APPLICATION_ERROR)`. We do not
do that: by the time the app re-raises it has already produced a 500 response, and an HTTP client
would simply have received it. Turning "the handler crashed" into "the transport failed" tells
the caller something different and less useful, and it would mean the two transports report the
same server bug in two different shapes. The traceback goes to the log instead.

Open question: should a 5xx *also* be visible as a transport-level failure for callers that only
watch for `RemoteError`? Currently it is not.

### Two viewsets on one base path are not reported

Routing picks the first match, so the second viewset's endpoints are silently unreachable. That
looks like it deserves a warning, and it had one for a while - until it turned out to fire 75
times in this project's own test suite, because building a throwaway viewset per test case is
indistinguishable from the real mistake. A warning that noisy is one nobody reads.

The registry does key on `module.qualname`, so a module reload replaces its registration cleanly
instead of duplicating it. The unreported case is genuinely two *different* viewsets colliding.
The REST side has always been silent about the same thing.

Better answer, if one is wanted: a `check_registrations()` an application can call at startup,
which reports collisions as an error where it can actually act on them, rather than a warning
emitted at import time.

### `register_rest` has no viewset-level or global switch

`@transports(rest=False)` works per endpoint, but there is no `route_viewset(register_rest=False)`
and no global default. Turning REST off for an entire viewset is what not calling `route_viewset`
already does, so the knob would have exactly one use: a viewset that wants muxws for everything
except a handful of endpoints. That did not seem worth a third resolution level. Easy to add if
it turns out to be wanted.

## Celery already carries the whole list pipeline

A design was proposed where the list parameters (filter, sort, pagination) are shipped to the
Celery worker, the worker reports back which of them it implemented, and the FastAPI side applies
whatever is left. Half of that turned out to be already true and the other half should not be
built.

`celery_viewset_client` patches the **route endpoints** - `list_items`, not `perform_list` - so the
entire pipeline already runs in the worker. Filter, sort and pagination parameters cross today and
work (verified against a live worker, and pinned by
`decorators/celery_viewset/list_params_test.py`), and the `PaginatedList` envelope comes back
intact.

Which means there is nothing to report back. `query.mark_applied()` is an intra-process contract,
and the worker *is* that process: a backend that translated part of the query into its own query
says so, and the in-memory stages skip that work, with both the query and the records in the same
memory. Reporting back to the FastAPI side would require shipping unfiltered, unsorted, unpaginated
records across the queue so they could be reduced somewhere else - the exact opposite of what
push-down is for.

The part of the proposal that survives intact is the filter plugin API: a Django-ORM-backed worker
still needs filters expressed as data rather than as hand-written closures before it can translate
them into queryset calls.

## Pagination and the fetch pipeline

### A separate mixin rather than two shapes on one endpoint

Paging was first built as an opt-in per request: no `limit` meant a plain list, a `limit` meant an
envelope, one endpoint returning `list[T] | PaginatedList[T]`. That is what the author's Django
implementation does, and it is backwards compatible in the strongest sense — no existing client
changes.

It was abandoned for two reasons. The soft one: every client has to branch on the shape it got
back, and the OpenAPI schema describes a union that no generator can do anything useful with. The
hard one: `list[T] | PaginatedList[T]` broke TypeVar resolution outright. `types.UnionType` is not
subscriptable, so the resolver silently returned the annotation unresolved, and pydantic collapses
`PaginatedList[T]` to the bare class inside a union's `get_args`. The schema came out describing
results as a list of anything, silently.

`PaginatedListMixin` makes it a viewset-wide decision instead: one endpoint, one shape, one schema.
The cost is that switching an existing viewset to paging is a breaking change for its clients,
rather than something they can adopt at their own pace.

### TypeVar resolution now knows about pydantic generics

`build_schema` and `resolve_typevars` were extended to handle parameterised pydantic models, which
are real classes rather than typing aliases and so were invisible to `get_origin`/`get_args`.
Without it `PaginatedList[T]` reached FastAPI unresolved. This is a general fix - any pydantic
generic in a return annotation was affected - but it was found because of pagination, and nothing
else in the codebase exercises it yet.

### `count` is best-effort

A list knows its length; a generator does not, and draining it to find out defeats the purpose. So
`count` is null for lazy sources. A backend that can count cheaply overrides `count_records()` -
the Django one answers with a `SELECT COUNT(*)` - so the null is a statement about the source, not
about the pipeline.

### Custom Vue endpoints changed idiom

They used to be written against `this.http` (axios); they should now be written against
`this.request()`, which each transport implements, so the same method body works on either.
`this.http` still exists on `RestProxyImpl` and still works. The docs were updated; anyone with
existing code has not been broken, only left on the REST-only idiom.

### The demo no longer uses Celery by default

It used to, unconditionally. That made the transport benchmark meaningless - queueing through Redis
and waiting for a worker dwarfs the difference between HTTP and a WebSocket frame. `DEMO_CELERY=1`
restores it, and `celery_worker.py` sets it itself. The Celery path is consequently no longer
exercised by simply running the demo, which is a real loss of coverage for it.

### Cursor pagination: the ordering key tuple is the whole design

Built. The concern recorded here - that a materialised `list[T]` leaves nothing to push a
comparison into - was answered by the pipeline rework and then by the filter API: the cursor
predicate is a `Filter`, so it reaches a backend through the same registry as everything else and
falls back to `matches()` when none can translate it.

Two things about it are decisions rather than facts, and both are arguable:

**The cursor is not signed.** Base64 over JSON is transport encoding, not protection. It carries
the ordering fields' values, so a client can read them and forge them. That is harmless when the
ordering is `(year, id)` and not when it is `(salary, id)`. An HMAC would fix it; nothing does
today.

**Filters are in the fingerprint.** A cursor issued under one filter is refused under another,
which means changing a filter restarts paging. That is defensible - the page you would get
otherwise is one nobody asked for - but it is stricter than it has to be: the position is still
well defined in the ordering regardless of the filter. Loosening it to bind only the ordering
would let a client narrow a filter without losing its place.
