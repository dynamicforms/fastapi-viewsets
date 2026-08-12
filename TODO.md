# TODO

## Pagination

`list_items` currently returns the entire collection in one response. The demo loads the whole
table at once, which makes it useless as a latency benchmark and unusable for real datasets.

Cursor pagination, not offset/limit. **Depends on the fetch pipeline rework below** — cursor
paging needs to push a comparison predicate down into whatever produces the rows, which the
current materialised-`list[T]` design cannot do. Do the pipeline first.

Design carried over from the author's Django implementation (concepts only, written fresh):

- The cursor encodes the **full n-tuple of ordering key values** of the anchor row, not just the
  first key. This is the central difference from DRF, which stores only the first ordering field
  and is therefore wrong for any genuinely multi-key ordering.
- The primary key is **appended to the ordering automatically** when not already present. That
  makes every key tuple unique, so ties are eliminated structurally and DRF's tie-offset
  machinery (with its hard ~1000 cap that collapses on low-cardinality sort columns) is not
  needed at all.
- Comparison is **lexicographic over the tuple**, expressed as a union of segments: key₁ strictly
  past the anchor; OR key₁ equal and key₂ strictly past; OR … Prefer a row-value comparison where
  the backend supports it — a single predicate a composite index can serve — and fall back to the
  segment union only where it cannot.
- Per-key direction is `XOR(field is descending, reading backwards)`, so mixed-direction ordering
  works.
- **NULL is defined as the smallest value**, always, in both directions, with explicit `IS NULL` /
  `IS NOT NULL` predicates rather than value comparisons.

Deliberate departures from the original:

- **Typed position.** Store real JSON (`null` is `null`) plus a type tag where the round trip is
  non-trivial (duration, decimal, tz-aware datetime, UUID). The original stringifies everything
  and uses a sentinel string for NULL, which collides with any real value equal to the sentinel.
- **Bind the cursor to the query.** Include a hash of the normalised ordering keys and active
  filters; on mismatch return 400 with a clear message instead of a `KeyError`/500. The original
  has no such check, so changing sort mid-pagination fails obscurely.
- **Separate "next page exists" from "polling anchor".** The original always offers `next` and
  `previous` so the client can re-poll both edges for newly inserted rows — but at the end of the
  list `next` becomes null, which loses exactly the anchor the design existed to provide. Model
  the two concepts separately and return explicit booleans rather than making the client infer
  from `null`.
- **Drop `offset` from the protocol.** With a unique position it is dead weight, and where it is
  honoured it degrades into the offset paging cursors exist to avoid.
- **Use the real PK name** from model metadata, not a literal `id`.

Also:

- Mirror it on the FE (`vue/rest-proxy.ts`, `vue/mixins.ts`): `list()` needs a paged variant.
- The demo grid should then load rows page by page instead of all at once.
- Check `@dynamicforms/vue-grid`'s incremental loading contract before fixing the response shape.

## Rework the fetch pipeline

The current design is `setup_filter` → `perform_list` → `filter_list`, and `setup_sort` →
`perform_list` → `sort_list` (`fastapi_viewsets/mixins.py:200-244`). It is wrong in two ways:

1. `perform_list` returns a materialised `list[T]`, so filtering, sorting and (soon) pagination
   can only ever happen in memory. A DB-backed viewset cannot push any of it down.
2. The `setup_*` hooks exist purely as a side channel to work around that — they mutate instance
   state before `perform_list` runs, so the pre/post pair has to be kept in sync by hand.

Target design: `perform_list` returns a **generator / lazy sequence** (for a DB-backed viewset,
whatever query object it wants). Filtering, sorting, grouping and pagination become
transformations over that object. Descendants override the default implementation and call it
hierarchically (`super()`), so each layer can either push the operation down into the query or
fall back to the in-memory default.

Open question: how extra transformations get declared — naming convention, explicit registration,
or plain overrides that chain via `super()`. Plain overrides are the simplest and probably
sufficient; the other two only earn their keep if transformations need to be composed
dynamically per request.

This is a breaking change for anyone who has overridden `filter_list` / `sort_list`, so it wants
a deprecation path.

## muxws transport

See `docs/guide/muxws.md` once written. Outstanding decisions captured during design:

- `/schema` must be able to report a *different* endpoint set per transport — an endpoint may be
  registered for muxws only, for REST only, or both.
- Response metadata (HTTP status) has to travel in muxws trailers, because muxws `data` frames
  carry no `headers` field (only `open` frames do). Verify this stays true if the muxws envelope
  ever gains per-data-frame headers.
- Reconnect: nothing survives a muxws reconnect (all in-flight streams fail with
  `ConnectionLost`). The proxy propagates that to the caller rather than retrying; revisit if
  transparent retry of idempotent calls turns out to be wanted.

## Upstream: muxws version mismatch

`ts/version.ts` in the muxws repo still exports `VERSION = '0.1.0'` while its `package.json` is
`0.2.0`; `ts/version.spec.ts` asserts they match (WSM-PKG-001), so that test should be failing.
`README.md` there also still says "0.1.0, alpha". Not our repo, but it bites anyone reading the
exported constant.
