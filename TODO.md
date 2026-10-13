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

## Conflicting updates of the same field

`DjangoORMViewSet` serialises updates of a row, so two updates of different fields both land and
`@model_validator`s see the stored values. Two updates of the same field still end with the later
one's value, and the earlier client is not told. Detecting that needs the client's view of the row
at write time:

- an opt-in version field on the viewset (`version_field = "version"`): `PUT`/`PATCH` carry the
  version they read, the update locks the row, compares, answers 409 on a mismatch and increments
  it on save; or ETag / `If-Match` on the same mechanism;
- an audit hook that records each write (who, when, the fields before and after), for resolving a
  conflict after the fact.

Both need a counterpart in the Vue mixins (sending the version, handling 409).
