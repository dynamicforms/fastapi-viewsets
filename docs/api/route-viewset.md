# route_viewset — API Reference

```python
from fastapi_viewsets.decorators.route_viewset import route_viewset
```

## Signature

```python
def route_viewset(
    router: APIRouter,
    base_path: str,
    lifecycle: LifecycleType = "singleton",
    pk_field_name: str | None = None,
    register_muxws: bool | None = None,
    register_rest: bool | None = None,
) -> Callable[[type[T]], type[T]]
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `router` | `APIRouter` | — | FastAPI router to register routes on, for REST and muxws alike; include it in the application |
| `base_path` | `str` | — | URL prefix for all endpoints |
| `lifecycle` | `LifecycleType` | `"singleton"` | Viewset instance lifecycle |
| `pk_field_name` | `str \| None` | `None` | PK field name; a `POST` request body leaves it out or gives it as null, any other value answers 422 |
| `register_muxws` | `bool \| None` | `None` | Whether this viewset's endpoints are published on the muxws transport. `None` defers to `settings.viewsets_register_muxws`, itself `True` by default |
| `register_rest` | `bool \| None` | `None` | Whether this viewset's endpoints are published on `router` as HTTP routes. `None` defers to "yes" |

`@transports(rest=..., muxws=...)` from `fastapi_viewsets.mux_ws` decides the same question for a
single endpoint. See
[Choosing which viewsets are published](../guide/muxws#choosing-which-viewsets-are-published).

## Return value

Returns a decorator; the decorator registers the class's routes and returns the class itself. The
class is modified in place: it gains a router of its own, carrying every route the viewset serves
rebased onto `base_path`; a FastAPI app that answers `GET {base_path}/schema` with the viewset's
own OpenAPI document; and `cls.__viewset_metadata__`, a dict of:

| Key | Value |
|-----|-------|
| `base_path` | the `base_path` argument |
| `lifecycle` | the `lifecycle` argument |
| `router` | the `APIRouter` passed in as the first argument, not the router the class gained |
| `pk_field_name` | the `pk_field_name` argument, `None` without it |
| `paths` | each action name, the name of the method serving the endpoint, mapped to its route's path relative to `base_path`: `""` for `list_items`, `"/{pk}"` for `retrieve`, `"/bulk"` for `bulk_create`, `"/schema"` for `schema` |
| `parent` | `None`; for a [sub-viewset](#viewsetrouter), the class it is mounted under |
| `sub_viewsets` | each path the class mounts a sub-viewset at mapped to the mounted class; `{}` for none |

A route's path on the application is the routed base path, the `include_router` prefix followed by
`base_path`, followed by `paths[action]`. Every endpoint added to `router` carries
`__viewset_class__`, the class, and `__viewset_action__`, the action name; the `/schema` endpoints
carry `"schema"`.

Applied as `@route_viewset(...)` on the class or called as `route_viewset(...)(SomeViewSet)`.

Each routing of a class builds its routes anew, for its own `base_path` and with its own singleton.
Decorating a class again, or mounting it a second time, replaces its `__viewset_metadata__` and its
muxws registration; the routes of the earlier routing keep serving. To serve one viewset on a second
route with metadata of its own, decorate a subclass, which gets endpoints, a singleton instance and
metadata of its own:

```python
class OrgTagsViewSet(TagsViewSet):
    pass

route_viewset(APIRouter(prefix="/orgs/{org_slug}"), "/tags", pk_field_name="id")(OrgTagsViewSet)
```

## Behaviour

- Collects the `__router` each class in the MRO declares for itself and rebases its routes onto `base_path`.
- Routes the viewsets the class mounts with [`ViewSetRouter.sub_viewset`](#viewsetrouter) on `router` before the
  class's own routes, each at `{base_path}/{path}`, recursively.
- Resolves all generic `TypeVar` parameters using the concrete types declared on the viewset class.
- Deduplicates routes by HTTP methods plus full path; the most derived class in the MRO wins, so a mixin can replace a
  route it would otherwise inherit.
- Registers routes most specific first: literal segments such as `bulk` and `lookup`, then `{pk}`, then the bare
  `base_path`; within one path the order is `DELETE`, `PATCH`, `PUT`, `POST`, `GET`.
- Adds `GET {base_path}/schema` once per transport, each answering with the OpenAPI document of the endpoints
  published on that transport. Each operation in it carries `x-viewset-action`, the name of the viewset method that
  serves it (`top_rated` for `GET {base_path}/top-rated`); the FE schema check matches a custom endpoint to a ViewSet
  method by it.
- Derives an OpenAPI tag from the class name (strips `ViewSet` suffix) and records the class's own docstring as that
  tag's description.
- Gives every route a dependency that runs each configured `Middleware`'s `depends()` before the request body is parsed,
  plus `settings.viewsets_security_scheme` when one is set. That dependency is the last entry of the route's
  `dependencies` and carries the attribute named by `MIDDLEWARE_DEPENDS_ATTRIBUTE`
  (`"__viewset_middleware_depends__"`, in `fastapi_viewsets.decorators.route_viewset`) set to `True`.
- Reads the filter model of an endpoint parameter annotated `Annotated[Model, Query()]` (a list's `TFilter`, a
  lookup's `TLookupFilter`) from the query string: every field of the model is a query parameter, documented with its
  type, default, description and constraints, under its alias when it has one. A sequence-typed field (`list[int]`)
  is read from a repeated key (`?ids=1&ids=2`). The sent parameters are validated into the model; a field the request
  does not send takes the model's default. A value that does not validate, against its field or against a validator
  of the model, is a 422 `fastapi-viewsets.validation_failed` with the failure in `field_errors` under the parameter
  name (`ids.1` for the second `ids` of `?ids=1&ids=x`).
- Drops the declared `response_model` from every route when a middleware in `settings.viewsets_command_middleware`
  declares `modifies_response_shape`, since that middleware may reshape the body.
- Raises `ValueError` when `lifecycle` is none of `"singleton"`, `"per-request"`, `"instance-key"`, when
  `@endpoint_docs` names an endpoint the class does not have, when a route of the class starts with a path it mounts a
  sub-viewset at, and when it mounts a class it is mounted under, itself included.
- Adds each endpoint to `router` once, as a route that matches only the transports it is published on (see
  [muxws](../guide/muxws#choosing-which-viewsets-are-published)); a route not published on REST is left out of the
  application's OpenAPI document. Records the endpoints published on muxws in the muxws registry.
- Runs every configured middleware's [`execute()`](../guide/command-middleware#around-the-execution-middleware-execute)
  around the call of the viewset method, inside the command middleware chain, unless the action is a
  `celery_viewset_client` method, which executes in the worker.
- Stores metadata on the class as `cls.__viewset_metadata__`.

## Lifecycle modes

| Value | Instance creation | State hooks |
|-------|------------------|-------------|
| `"singleton"` | Once, at decoration time | `load_state()` / `save_state()` called on every request. No locking — concurrent requests may race. |
| `"per-request"` | Once per incoming HTTP request | Not called. |
| `"instance-key"` | Once per request | `load_state()` / `save_state()` called on every request. No locking — concurrent requests may race. |

See [ViewSet Lifecycle](../guide/lifecycle) for the full guide, including a worked example.

## ViewSetRouter

```python
from fastapi_viewsets.decorators import SubViewSet, ViewSetRouter
```

`ViewSetRouter` is an `APIRouter` that also mounts viewsets under the base path of the viewset that
declares it as its `__router`. See [Nested Viewsets](../guide/nested-viewsets).

```python
class ViewSetRouter(APIRouter):
    sub_viewsets: dict[str, SubViewSet]

    def sub_viewset(
        self,
        path: str,
        viewset: type | object,
        *,
        lifecycle: LifecycleType = "singleton",
        pk_field_name: str | None = None,
        register_muxws: bool | None = None,
        register_rest: bool | None = None,
        action_configuration: dict | None = None,
    ) -> SubViewSet
```

`sub_viewset` records the mount in `sub_viewsets` under `path` and returns it. `route_viewset` routes
the mounted viewset when it routes the declaring class, at `{base_path}/{path}`, with `lifecycle`,
`pk_field_name`, `register_muxws` and `register_rest` as `route_viewset` takes them; `register_muxws`
and `register_rest` of `None` take the values the parent is routed with. `viewset` is a viewset class
or an instance of one, used as the singleton.

| Raises | When |
|--------|------|
| `ValueError` | `path`, without leading and trailing `/`, is not one path segment of letters, digits, `_`, `-`, `.` and `~`; is `schema`; is already mounted on this router. `lifecycle` is unknown, or other than `"singleton"` for an instance |
| `TypeError` | `viewset` is neither a class nor an instance with a `__dict__` |

A route of a sub-viewset carries, after the application's, `include_router`'s, the router's and its
own dependencies, one dependency per viewset it is mounted under, the outermost first, each with the
attribute `SUB_VIEWSET_GATE_ATTRIBUTE` (`"__viewset_sub_viewset_gate__"`, in
`fastapi_viewsets.decorators.route_viewset`) set to `(parent class, path)`, and its own
`MIDDLEWARE_DEPENDS_ATTRIBUTE` dependency last. A gate dependency runs
`settings.viewsets_security_scheme` when one is set, the context processors and `depends()` of the
effective middlewares, with the parent class as the viewset, `path` as the action name and the
parent's class-level `@action_configuration` with `action_configuration` merged over it as the
configuration, against a context built for it alone.

A sub-viewset's routes take the OpenAPI tags of the viewset at the top of its mounts, and its class
docstring registers no tag. A route without a summary of its own is summarised as the mounted paths,
title-cased and joined by ` / `, a colon and FastAPI's default summary: `Members: List Items`,
`Members / Active: List Items`.

### SubViewSet

A frozen dataclass, one mount:

| Field | Type | Meaning |
|-------|------|---------|
| `path` | `str` | The sub-path, one segment; the parent's action name for its checks |
| `viewset` | `type` | The mounted class |
| `instance` | `object \| None` | The instance passed to `sub_viewset`; `None` for a class |
| `lifecycle` | `LifecycleType` | As passed |
| `pk_field_name` | `str \| None` | As passed |
| `register_muxws` | `bool \| None` | As passed; `None` takes the parent's value |
| `register_rest` | `bool \| None` | As passed; `None` takes the parent's value |
| `action_configuration` | `dict` | The configuration of the parent's action `path`, `{}` without one |

`fastapi_viewsets.decorators.sub_viewsets` also has `sub_viewsets_of(cls) -> dict[str, SubViewSet]`,
the mounts of `cls` and of the classes in its MRO, a more derived class's mount replacing one at the
same path, and `mounted_under(cls) -> Iterator[type]`, every class mounted under `cls`, directly or
through further mounts, once each.
