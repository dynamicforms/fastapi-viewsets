# Nested Viewsets

A viewset can mount other viewsets under sub-paths of its base path. Each mounted viewset, a
*sub-viewset*, is a complete viewset of its own: its own endpoints, list shapes, filter model,
lifecycle, `/schema` and muxws registration. A request to it passes the parent's checks first, then
its own.

A sub-viewset serves another view of the parent's data at a path under the parent: an `Account`
viewset at `/accounts` and, at `/accounts/members`, a paginated list of `MemberRow`, a slimmer row
with a computed column. Each list endpoint has one row type; a second row type is a second viewset.

---

## Declaring a sub-viewset

The parent declares a `ViewSetRouter` as its own `__router` and mounts the sub-viewset on it in the
class body:

```python
from django.db.models import Count
from fastapi import APIRouter
from pydantic import BaseModel

from fastapi_viewsets.backends.django_orm import DjangoORMViewSet
from fastapi_viewsets.decorators import route_viewset, ViewSetRouter
from fastapi_viewsets.mixins import make_all_optional, PaginatedListMixin, ViewSetMixin


class Account(BaseModel):
    id: int
    name: str
    email: str


class MemberRow(BaseModel):
    id: int
    name: str
    group_count: int


class MembersViewSet(DjangoORMViewSet[int, MemberRow], PaginatedListMixin[MemberRow, make_all_optional(MemberRow)]):
    """Members of the club, one row per account."""

    model = AccountModel
    schema = MemberRow

    async def perform_list(self, context):
        return self.get_queryset(context).annotate(group_count=Count("groups"))


router = APIRouter()


@route_viewset(router, "/accounts", pk_field_name="id")
class AccountViewSet(DjangoORMViewSet[int, Account], ViewSetMixin[int, Account]):
    model = AccountModel
    schema = Account

    __router = ViewSetRouter()
    __router.sub_viewset("members", MembersViewSet, pk_field_name="id")
```

`GET /accounts/members` answers with a page of `MemberRow`. `to_record` builds each row from the
annotated Django row, annotations included. A filter or sort on an annotation is applied in memory;
one on a model field is pushed down to the database.

`ViewSetRouter` is an `APIRouter`: custom endpoints are declared on it as on any `__router`
([Custom Endpoints](./custom-endpoints)). A viewset that mounts nothing declares a plain
`APIRouter`, or no `__router` at all.

`sub_viewset(path, viewset, **options)` takes:

| Argument | Default | Meaning |
|----------|---------|---------|
| `path` | — | The sub-path, one path segment of letters, digits, `_`, `-`, `.` and `~`. It is also the name of the parent's action whose checks a request to the sub-viewset passes |
| `viewset` | — | A viewset class, or an instance of one, which is then the singleton: a viewset built with constructor arguments or by a factory classmethod |
| `lifecycle` | `"singleton"` | As for `route_viewset`; an instance takes `"singleton"` only |
| `pk_field_name` | `None` | As for `route_viewset` |
| `register_muxws` | `None` | As for `route_viewset`; `None` takes the value the parent is routed with |
| `register_rest` | `None` | As for `route_viewset`; `None` takes the value the parent is routed with |
| `action_configuration` | `None` | Configuration of the parent's action `path`; see [Checks](#checks) |

Code the parent and the sub-viewset share, a queryset scope or a conversion, goes into a common base
class or mixin of the two.

A mount on a base class is inherited, and a subclass that mounts another viewset at the same path
replaces it. Sub-viewsets nest: a sub-viewset can mount viewsets of its own, at
`/accounts/members/active` and further down.

---

## Routing order

`route_viewset` routes the sub-viewsets on the parent's router ahead of the parent's own routes.
`GET /accounts/members` reaches the sub-viewset; `GET /accounts/{pk}` with a pk of `members` is
never tried. The order is the same on REST and on muxws, which share the application's router.

A parent route whose first path segment is a mounted path, `GET /accounts/members` declared as a
custom endpoint next to a mount at `members`, raises `ValueError` when the parent is routed. So does
a mount at `schema`, and a mount of a viewset the parent is itself mounted under.

---

## Checks

A request to an endpoint of the sub-viewset passes two sets of checks, the parent's first:

1. **The parent's checks**, with the parent class as the viewset and the mount's path as the action
   name (`members`): `settings.viewsets_security_scheme`, the context processors and
   `Middleware.depends()` of every effective middleware. The action's configuration is the parent's
   class-level `@action_configuration` with `sub_viewset(..., action_configuration=...)` merged over
   it, so a middleware reads it with `config_from(context)` as for any action of the parent.
2. **The sub-viewset's own checks**, exactly as for a viewset routed with `route_viewset`: its
   security scheme dependency, `depends()`, context processors, the command middleware chain and
   `execute()`, with the sub-viewset class and its own action (`list_items`).

What either set refuses is refused; a refusal of the parent's checks ends the request before the
sub-viewset's checks run. A nested sub-viewset passes the checks of every viewset above it, the
outermost first.

```python
@action_configuration({Authorization: lambda request, cls, context: is_admin(context)})
@route_viewset(router, "/accounts", pk_field_name="id")
class AccountViewSet(...):
    __router = ViewSetRouter()
    __router.sub_viewset(
        "members",
        MembersViewSet,
        action_configuration={Authorization: lambda request, cls, context: is_club_member(context)},
    )
```

An administrator passes the parent's checks for the parent's own endpoints, a club member passes
them for `/accounts/members`.

The context of the parent's checks is built for them alone. The sub-viewset's context is built for
the sub-viewset class and its action as for any request, and holds nothing from the parent's
context. A value the parent's checks establish reaches the sub-viewset through the request, for
example `request.state`.

Each check is a dependency of the sub-viewset's routes: the application's and the router's
dependencies come first, then one dependency per parent (carrying `SUB_VIEWSET_GATE_ATTRIBUTE`), then
the sub-viewset's own (`MIDDLEWARE_DEPENDS_ATTRIBUTE`). The [events gate run](./events#the-gate-run)
of a sub-viewset's topic resolves the route's dependencies, so the parent's checks admit or refuse a
subscription to it as they do a request.

---

## Events

A sub-viewset publishes and is subscribed to under its own topic, its routed path
(`/accounts/members`). To share the parent's events, it declares the parent's
[event scope](./events#scoped-topics) name explicitly:

```python
@event_scope(club_scope, name="accounts")
class MembersViewSet(...): ...


@event_scope(club_scope, name="accounts")
@route_viewset(router, "/accounts", pk_field_name="id")
class AccountViewSet(...):
    __router = ViewSetRouter()
    __router.sub_viewset("members", MembersViewSet)
```

Both topics are then stored under `@accounts:{key}`: a write on `/accounts` reaches the subscribers
of `/accounts/members`, which fetch the changed rows from the sub-viewset
([`watch()` with a `fetch` function](./vue-events#watch-on-a-viewset)). A sub-viewset does not take
the parent's scope without declaring it.

`event_scope` accepts a name another class declared for a class mounted under that class or mounting
it, directly or through further sub-viewsets, and for a subclass of such a class. The mounts are read
from the classes' `ViewSetRouter`s, so the order of the decorators does not matter: the sub-viewset
is defined, and usually decorated, before the parent that mounts it. An unrelated class declaring the
name still raises `ValueError`.

---

## OpenAPI

The sub-viewset's operations are listed in the parent's tag group, at their sub-paths:
`GET /accounts/members` is under `Account` in `/docs` and `/redoc`. The sub-viewset's class docstring
does not open a group of its own. An operation without an explicit summary is summarised with the
mount's path in front of FastAPI's default, `Members: List Items`, so it is told apart from the
parent's `List Items`; a summary given with `@endpoint_docs` or on a custom route is kept as written.
Operation ids are FastAPI's, unique by path.

The sub-viewset answers `GET /accounts/members/schema` with an OpenAPI document of its own endpoints;
the parent's `/schema` does not list them. Both are registered for muxws through the same
registration as a viewset routed with `route_viewset`.

---

## Metadata

`__viewset_metadata__` of a sub-viewset carries its full base path (`/accounts/members`), the
router it is routed on, which is the parent's, its own `paths`, and the class it is mounted under as
`parent`. The parent's `sub_viewsets` maps each mounted path to the mounted class. See
[route_viewset](../api/route-viewset#return-value).

A class routed again, by a second mount or a second `route_viewset`, gets routes of its own for that
routing, with that routing's base path and singleton; its `__viewset_metadata__` describes the last
routing.

---

## Celery

A sub-viewset can be a [`celery_viewset`](./routers#celery_viewset) with its own task prefix. Both
sets of checks and the command middleware chain run in the FastAPI process, the action executes in
the worker under `Middleware.execute()`, as for any `celery_viewset`.

---

## Frontend

The sub-viewset is an ordinary ViewSet on the frontend, with its own base path:

```ts
class MemberApi extends restViewSet<MemberRow>()('id', [PaginatedListMixin]) {}

const members = new MemberApi({ basePath: '/accounts/members', events });
const page = await members.listPage({ limit: 50 });
```
