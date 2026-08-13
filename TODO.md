# TODO

## Filter operators still to translate

`overlaps` has no Django compiler: `ArrayField.overlap` is Postgres-only, so a request touching it
falls back to in-memory filtering. Worth adding a Postgres-specific compiler once there is a
Postgres backend to hang it off.

## Grid integration

`@dynamicforms/vue-grid`'s incremental loading contract has not been checked against `listPage()`.
The demo drives paging with explicit prev/next buttons rather than guessing at it. If the grid
expects to drive loading itself, `listPage` may need a different shape or an adapter.

## Deprecation removal

`setup_filter` / `setup_sort` warn but still work. Decide a version to drop them in.

## Cursor pagination: what is not done

`CursorListMixin` is in. Left over:

- **Signing.** The cursor is base64 over JSON, which is transport encoding rather than protection:
  it carries ordering-field values, so a client can read them and edit them. Where the ordering
  fields are sensitive it wants an HMAC, or encryption.
- **Row values need the right index.** The fast path only pays off with a composite index on
  exactly the ordering columns, in order. Nothing checks or warns; a viewset can page correctly and
  slowly without anyone noticing.
- **Mutable ordering keys.** If the value of a column you are ordering by changes between two
  pages, that row can be visited twice or not at all. The PK tiebreaker bounds the damage to that
  row; nothing else can. Worth documenting rather than solving.
- **Django `DESC NULLS` is still declined.** `apply_sort` refuses to push down any descending
  ordering, so a cursor page sorted descending sorts in memory. `F(col).desc(nulls_last=True)`
  would fix it per database backend.
- **The demo does not use it.** `/music` and `/music-db` still page by offset, which is the right
  default for a grid that shows page numbers; a third endpoint would show the cursor off better
  than converting one of those.
