# TODO

## Cursor pagination

Offset paging is in (`PaginatedListMixin`). Cursor paging is not, and it is the one that actually
fixes the two problems offset paging has: it re-reads every skipped row, and it drifts when records
are inserted or deleted between pages.

The blocker is not the algorithm, it is where the algorithm runs. A cursor works by pushing a
comparison predicate down into whatever produces the rows. `apply_*` stages and `query.applied` now
give it somewhere to live for an in-process backend, but for a Celery-backed viewset the query has
to be composed *in the worker*, which means the cursor state has to travel there. That part is
undesigned.

In-memory viewsets do not need any of this — slicing a list is what they do — so an implementation
that only ever runs in-process could ship first and would still be useful.

Design carried over from the author's Django implementation (concepts only, to be written fresh):

- The cursor encodes the **full n-tuple of ordering key values** of the anchor row, not just the
  first key. This is the central difference from DRF, which stores only the first ordering field
  and is therefore wrong for any genuinely multi-key ordering.
- The primary key is **appended to the ordering automatically** when not already present. That
  makes every key tuple unique, so ties are eliminated structurally and DRF's tie-offset machinery
  (with its hard ~1000 cap that collapses on low-cardinality sort columns) is not needed at all.
- Comparison is **lexicographic over the tuple**: key₁ strictly past the anchor; OR key₁ equal and
  key₂ strictly past; OR … Prefer a row-value comparison where the backend supports it — a single
  predicate a composite index can serve — and fall back to the segment union only where it cannot.
- Per-key direction is `XOR(field is descending, reading backwards)`, so mixed-direction ordering
  works.
- **NULL is defined as the smallest value**, always, in both directions, with explicit `IS NULL` /
  `IS NOT NULL` predicates rather than value comparisons.

Deliberate departures from the original:

- **Typed position.** Store real JSON (`null` is `null`) plus a type tag where the round trip is
  non-trivial (duration, decimal, tz-aware datetime, UUID). The original stringifies everything and
  uses a sentinel string for NULL, which collides with any real value equal to the sentinel.
- **Bind the cursor to the query.** Include a hash of the normalised ordering keys and active
  filters; on mismatch return 400 with a clear message instead of a `KeyError`/500. The original has
  no such check, so changing sort mid-pagination fails obscurely.
- **Separate "next page exists" from "polling anchor".** The original always offers `next` and
  `previous` so the client can re-poll both edges for newly inserted rows — but at the end of the
  list `next` becomes null, which loses exactly the anchor the design existed to provide.
- **Drop `offset` from the protocol.** With a unique position it is dead weight, and where it is
  honoured it degrades into the offset paging cursors exist to avoid.
- **Use the real PK name** from model metadata, not a literal `id`.

## Grid integration

`@dynamicforms/vue-grid`'s incremental loading contract has not been checked against `listPage()`.
The demo drives paging with explicit prev/next buttons rather than guessing at it. If the grid
expects to drive loading itself, `listPage` may need a different shape or an adapter.

## muxws: response headers

Response status currently rides in the stream's trailers, because a muxws `data` frame has no
`headers` field. That is free for a unary reply but wrong for a streaming one, where the caller
would have to consume the whole body before learning the status.

muxws is expected to grow response headers on data frames. When it does:

- flip `RESPONSE_META_VIA_TRAILERS` in `fastapi_viewsets/mux_ws/protocol.py`
- update the matching branch in `vue/muxws-proxy.ts`
- the client can then use `peer.request()` instead of `open()` + `result()` + `closed`, since it
  will no longer need the Stream object just to read trailers

## Deprecation removal

`setup_filter` / `setup_sort` warn but still work. Decide a version to drop them in.
