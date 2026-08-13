# TODO

## Filter operators still to translate

`overlaps` has no Django compiler: `ArrayField.overlap` is Postgres-only, so a request touching it
falls back to in-memory filtering. Worth adding a Postgres-specific compiler once there is a
Postgres backend to hang it off.

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
- **Offset paging has no demo.** Both demo endpoints are cursor-paged now, so `PaginatedListMixin`
  is exercised only by tests.
- **`vue-grid` ships typings its exports map hides.** `package.json` has no `types` condition, so
  TypeScript cannot find `dist/index.d.ts` through the `.` export. The demo works around it with a
  `paths` entry; the real fix belongs upstream.
