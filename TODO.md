# TODO

## Filter operators still to translate

`overlaps` has no Django compiler: `ArrayField.overlap` is Postgres-only, so a request touching it
falls back to in-memory filtering. Worth adding a Postgres-specific compiler once there is a
Postgres backend to hang it off.

## Cursor pagination: what is not done

`CursorListMixin` is in. Left over:

- **Row values need the right index.** The fast path only pays off with a composite index on
  exactly the ordering columns, in order. Nothing checks or warns; a viewset can page correctly and
  slowly without anyone noticing.
- **Mutable ordering keys.** If the value of a column you are ordering by changes between two
  pages, that row can be visited twice or not at all. The PK tiebreaker bounds the damage to that
  row; nothing else can. Worth documenting rather than solving.
- **No demo of a viewset pinned to offset paging.** The demo's viewsets default to `cursor` and
  offer the other two through `X-List-Shape`, so offset paging is reachable but never the declared
  shape. That is the more interesting configuration to show; it does leave `list_shape =
  "paginated"` on its own exercised only by tests.
