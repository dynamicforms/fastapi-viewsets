# GAPS

Decisions that could go the other way, and the reasoning behind the way they went. Each is a thing
to argue about, not a thing that is settled. Entries that were argued about and settled are kept
where the reasoning is worth having on record, and marked as such.

## Still open

### The synthetic ASGI request costs a JSON round trip

`process_command` builds an ASGI scope and calls the FastAPI app rather than reaching into
`lifecycle_runner` directly. That buys total parity: parameter binding, validation, `Depends`,
command middleware, context processors, response models and status codes are the same code on both
transports, so they cannot drift.

It costs a decode/re-encode. muxws has already decoded the payload; we re-encode it to bytes for
FastAPI to parse, and decode FastAPI's response bytes again to hand back. Measured on a 50-record
page:

| | per call | share |
|---|---|---|
| whole muxws command | 0.457 ms | |
| the ASGI dispatch itself | 0.252 ms | 55% |
| decoding the response | 0.068 ms | 15% |
| encoding a 50-record request body | 0.076 ms | 17% |

A read pays about 15% for the round trip; a large write about a third. Real, and not where the time
goes: over half the call is FastAPI's own dispatch, which is precisely what the parity is bought
with. The honest fix is not to bypass FastAPI but to let it accept a pre-decoded body, and nothing
here is worth doing before that exists.

### A third transport wants a different parameter, not a third `register_*`

`route_viewset` takes `register_rest` and `register_muxws`. A third transport would make it three,
which is the shape that stops scaling.

A set of flags — `{REGISTER_REST, NO_REGISTER_MUXWS}` — is the worse of the two options: the
setting is tri-state (yes / no / defer), so a set needs a paired positive and negative member per
transport and can express the contradiction of holding both. A mapping cannot:
`transports={"rest": False}`, absent key meaning defer, is tri-state natively and grows by one key
per transport rather than two members.

Not built, because a third transport does not exist and may never — muxws would likely absorb a
Unix socket rather than sit beside one. When it does, the two `register_*` parameters become
shorthand for the mapping rather than being removed.

### `count` is best-effort

A list knows its length; a generator does not, and draining it to find out defeats the purpose, so
`count` is null for lazy sources. A backend that can count cheaply overrides `count_records()` —
the Django one answers with a `SELECT COUNT(*)` — so the null is a statement about the source, not
about the pipeline. A cursor page never counts at all, deliberately.

### OpenAPI cannot say that the response shape depends on a request header

A viewset offering several shapes documents them as an `anyOf`, with one named example per shape so
the docs say `plain` rather than `ListOf_MusicTrack_`. What no part of the schema can state is that
`X-List-Shape` selects between them — OpenAPI has no way to express a response that varies by
request header. That stays prose in the endpoint's description, and a generated client gets a union
it has to narrow itself.

Which is why declaring a single shape stays the recommended default rather than the fallback.

### The demo no longer exercises Celery by default

It used to, unconditionally, which made the transport benchmark meaningless: queueing through Redis
and waiting for a worker dwarfs the difference between HTTP and a WebSocket frame. `demo.py
--celery` turns it back on and an e2e spec covers that path, skipped when Redis is absent. So the
path is covered — just not by simply running the demo.

## Settled, kept for the reasoning

### An unhandled exception answers 500 rather than resetting the stream

muxws' own idiom is that a handler which raises produces `reset(APPLICATION_ERROR)`. This does not.
By the time the app re-raises it has already produced a 500, and an HTTP client would simply have
received it: a status is an answer, not a transport failure. Resetting would tell the caller the
connection misbehaved when the server in fact answered, and would make the two transports report
the same server bug in two different shapes. The traceback goes to the log instead.

### Two viewsets on one base path warn rather than raise

Routing picks the first match, so the second viewset's endpoints are unreachable, and neither
FastAPI nor this library would otherwise say so. A warning rather than an error, because that is
proportionate to how easily it is fixed and because refusing would be a new way for an application
that starts today to stop starting.

It was briefly removed for firing 75 times in this project's own suite — test scaffolding is
indistinguishable from the real mistake. That was letting test convenience decide a production
question; the suite filters it in `pyproject.toml` instead.

### Response status travels in leading headers

It rode in `trailers` until muxws 0.3.1, which carries `headers` on the first `data` frame a peer
sends as well as on `open`. Trailers only attach to a frame with `end: true`, so the status arrived
*after* the body — free for a unary reply, wrong for a streaming one, where a caller would have had
to read a whole response to discover it was an error.

### The cursor is not signed

Base64 over JSON is transport encoding, not protection: a client can read the ordering values and
forge them. Dismissed on argument — the cursor only ever contains values from rows that client was
already served, and the queryset it indexes into still comes from `get_queryset(context)`, so
tampering moves you around inside data you could already reach, which a filter would do more
easily. Sign it only if the ordering fields are themselves sensitive.

### Filters are in the cursor's fingerprint

A cursor issued under one filter is refused under another, so changing a filter restarts paging.
Stricter than strictly necessary — the position is still well defined in the ordering — but it
matches what a filter change already did, and the page you would otherwise get is one nobody asked
for.

### One list endpoint can serve several shapes

Paging was first built as an opt-in per request, then abandoned, then rebuilt. The lesson is in the
middle step.

The soft reason to abandon it was that clients must branch on the shape they got. The hard one was
that `list[T] | PaginatedList[T]` broke TypeVar resolution outright: `types.UnionType` is not
subscriptable, so the resolver handed back the annotation unresolved, and pydantic collapsed
`PaginatedList[T]` to the bare class inside a union's `get_args`. The schema silently described
results as a list of anything.

That blocker was removed as a side effect of later work — `PaginatedList` moved to its own TypeVar
and `resolve_typevars` learned about pydantic generics — and nobody went back to re-examine the
decision it had forced, until it came up again in conversation. `typing.Union` (not `X | Y`) now
resolves correctly, so a viewset can declare `list_shapes` and offer all three.

Worth keeping: a decision forced by a technical limit needs revisiting when the limit goes, and
nothing prompts that on its own.

### Celery already carried the whole list pipeline

A design was proposed where the list parameters are shipped to the worker, the worker reports back
which it implemented, and FastAPI applies the rest. Half was already true and the other half should
not be built.

`celery_viewset_client` patches the route endpoints — `list_items`, not `perform_list` — so the
entire pipeline already runs in the worker, and filter, sort and pagination parameters already
cross. Which means there is nothing to report back: `query.mark_applied()` is an intra-process
contract and the worker *is* that process. Reporting back would mean shipping unreduced records
across the queue to be reduced somewhere else, which is the opposite of what push-down is for.
