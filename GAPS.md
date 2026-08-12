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

### Response status travels in trailers

Per SPEC §2.2 a muxws `data` frame has no `headers` field; `headers` is an `open`-frame thing and
the only post-body channel is `trailers`, which may only ride a frame with `end: true`. For a
unary reply that is free - the trailers arrive in the same frame as the body - but a streaming
response would force the caller to consume the whole body before learning the status.

muxws is expected to grow response headers on data frames. `protocol.RESPONSE_META_VIA_TRAILERS`
marks the single place that changes when it does, and the TypeScript client has the matching
branch.

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
`count` is null for lazy sources. A backend that can count cheaply (a `SELECT COUNT(*)`) has no way
to say so yet - `apply_pagination` would have to be overridden wholesale. A `count_records()` hook
would be the obvious addition if anyone wants it.

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

### Cursor pagination needs a query object that does not exist yet

Cursor paging works by pushing a comparison predicate down into whatever produces the rows. With
`perform_list` returning a materialised `list[T]`, there is nothing to push into - we would load
everything and then slice, which is all of the cost and none of the benefit.

For an in-memory viewset that is fine and always will be: slicing a list is what it does. For a
Celery-backed or DB-backed viewset it is not, and the predicate has to reach the worker so the
query is composed there. That is the part still to be designed - see TODO.md.

Plain limit/offset paging does *not* have this problem: it is a trim, and Django's ORM (or a
generator, or a list) can all do it. So the two can ship separately, limit/offset first.
