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
| `router` | `APIRouter` | — | FastAPI router to register routes on |
| `base_path` | `str` | — | URL prefix for all endpoints |
| `lifecycle` | `LifecycleType` | `"singleton"` | Viewset instance lifecycle |
| `pk_field_name` | `str \| None` | `None` | PK field name; strips it from `POST` request body |
| `register_muxws` | `bool \| None` | `None` | Whether this viewset's endpoints are published on the muxws transport. `None` defers to `settings.viewsets_register_muxws`, itself `True` by default |
| `register_rest` | `bool \| None` | `None` | Whether this viewset's endpoints are published on `router` as HTTP routes. `None` defers to "yes" |

`@transports(rest=..., muxws=...)` from `fastapi_viewsets.mux_ws` decides the same question for a
single endpoint. See
[Choosing which viewsets are published](../guide/muxws#choosing-which-viewsets-are-published).

## Return value

Returns a decorator; the decorator registers the class's routes and returns the class itself. The
class is modified in place: it gains a router of its own, carrying every route the viewset serves
rebased onto `base_path`; a FastAPI app that answers `GET {base_path}/schema` with the viewset's
own OpenAPI document; and `cls.__viewset_metadata__`, a dict of `base_path`, `lifecycle` and
`router`, where `router` is the `APIRouter` passed in as the first argument rather than the router
the class gained. Applied as `@route_viewset(...)` on the class or called as
`route_viewset(...)(SomeViewSet)`.

## Behaviour

- Collects the `__router` each class in the MRO declares for itself and rebases its routes onto `base_path`.
- Resolves all generic `TypeVar` parameters using the concrete types declared on the viewset class.
- Deduplicates routes by HTTP methods plus full path; the most derived class in the MRO wins, so a mixin can replace a route it would otherwise inherit.
- Registers routes most specific first: literal segments such as `bulk` and `lookup`, then `{pk}`, then the bare `base_path`; within one path the order is `DELETE`, `PATCH`, `PUT`, `POST`, `GET`.
- Adds `GET {base_path}/schema`, answering with the OpenAPI document of the viewset's own FastAPI app.
- Derives an OpenAPI tag from the class name (strips `ViewSet` suffix) and records the class's own docstring as that tag's description.
- Gives every route a dependency that runs each configured `Middleware`'s `depends()` before the request body is parsed, plus `settings.viewsets_security_scheme` when one is set.
- Drops the declared `response_model` from every route when `settings.viewsets_command_middleware` is non-empty, since middleware may reshape the body.
- Raises `ValueError` when `@endpoint_docs` names an endpoint the class does not have.
- Hands the routes that resolve to muxws — every one but `/schema`, for which the registry substitutes its own — to the muxws registry.
- Stores metadata on the class as `cls.__viewset_metadata__`.

## Lifecycle modes

| Value | Instance creation | State hooks |
|-------|------------------|-------------|
| `"singleton"` | Once, at decoration time | `load_state()` / `save_state()` called on every request. No locking — concurrent requests may race. |
| `"per-request"` | Once per incoming HTTP request | Not called. |
| `"instance-key"` | Once per request | `load_state()` / `save_state()` called on every request. No locking — concurrent requests may race. |

See [ViewSet Lifecycle](../guide/lifecycle) for the full guide, including a worked example.
