# TODO

## Cursor pagination

Offset paging is in (`PaginatedListMixin`). Cursor paging is not, and it is the one that actually
fixes the two problems offset paging has: it re-reads every skipped row, and it drifts when records
are inserted or deleted between pages.

The blocker is not the algorithm, it is expressing the predicate as data. A cursor works by pushing
a comparison down into whatever produces the rows, and `apply_*` plus `query.applied` already give
it somewhere to live.

The Celery half of this is a non-problem: the whole pipeline already runs in the worker (see
GAPS.md), so the cursor is composed there, next to the data. What it needs is the filter plugin API
below - a cursor is just one more declarative predicate, which is why it comes after it.

The filter plugin API is the natural home: **a cursor predicate is just a filter**. It has a
`matches()` for the in-memory case, and a compiler per backend that emits either the row-value form
or the segment union. Nothing new is needed to carry it, and it inherits the all-or-nothing
push-down rule for free.

In-memory viewsets do not need any of this — slicing a list is what they do — so an implementation
that only ever runs in-process could ship first and would still be useful.

Design carried over from the author's Django implementation (concepts only, to be written fresh):

- The cursor encodes the **full n-tuple of ordering key values** of the anchor row, not just the
  first key. This is the central difference from DRF, which stores only the first ordering field
  and is therefore wrong for any genuinely multi-key ordering.
- The primary key is **appended to the ordering automatically** when not already present. That
  makes every key tuple unique, so ties are eliminated structurally and DRF's tie-offset machinery
  (with its hard ~1000 cap that collapses on low-cardinality sort columns) is not needed at all.
- Comparison is **lexicographic over the tuple**, and **both forms have to be implemented**:
  - **Row-value comparison** — `WHERE (year, id) > (2026, 5)` — one predicate a composite index
    serves directly. Only usable when *every* key sorts the same way, because a row constructor
    cannot express `a ASC, b DESC`.
  - **Segment union** — key₁ past the anchor; OR key₁ equal and key₂ past; OR … — for mixed
    directions, and for backends with no row values at all. Slower (the planner usually degrades
    it to a scan) but always correct.

  Verified against Django 5.2 + SQLite: the widely-cited `Func(..., function='ROW')` recipe is
  Postgres-only (`no such function: ROW`), and even a portable `(a, b)` constructor fails if you
  annotate it (`row value misused`). What works is one boolean expression used directly as a
  filter condition:

      class RowValues(Func):
          template = "(%(expressions)s)"
          arg_joiner = ", "

      class RowGt(Func):
          template = "%(expressions)s"
          arg_joiner = " > "
          output_field = BooleanField()

      queryset.filter(RowGt(RowValues(F("a"), F("b")), RowValues(Value(1), Value(2))))
      # -> WHERE ("a", "b") > (1, 2)

  Support: Postgres yes, SQLite yes (3.15+), MySQL yes, SQL Server no.
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
- **Anchor on records that are actually in the page, not on the boundary between pages.** DRF's
  `next`/`previous` encode a position *outside* the returned page. That leaves a blind spot: rows
  inserted between the previous page's last record and this page's first record fall in the gap
  between where `previous` points and where the page starts, and paging back skips them entirely.

  Return `first` and `last` instead — the key tuples of the page's own first and last records.
  Forward paging reads `> last`, exclusive, so no duplicate. Backward paging and polling read
  `<= first`, **inclusive**, which costs one duplicate row (`first` itself, which the client
  drops) and in exchange cannot skip anything inserted just before it, because the anchor is a row
  that exists rather than a boundary that moves.

  This also subsumes the "polling anchor" problem: `first` and `last` are properties of the page,
  so they are present whenever the page is non-empty, whether or not more data exists in that
  direction. Keep `next`/`previous` as the convenience links; make `first`/`last` available
  alongside them.
- **Drop `offset` from the protocol.** With a unique position it is dead weight, and where it is
  honoured it degrades into the offset paging cursors exist to avoid.
- **Use the real PK name** from model metadata, not a literal `id`.

## Filter operators still to translate

`overlaps` has no Django compiler: `ArrayField.overlap` is Postgres-only, so a request touching it
falls back to in-memory filtering. Worth adding a Postgres-specific compiler once there is a
Postgres backend to hang it off.

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
