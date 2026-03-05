# route_viewset decorator

`route_viewset` is the glue between your viewset class and a FastAPI router. It collects all routes defined by the mixin hierarchy, resolves generic type parameters, and registers them on the given router.

## Signature

```python
def route_viewset(
    router: APIRouter,
    base_path: str,
    lifecycle: Literal["singleton", "per_request"] = "singleton",
    pk_field_name: str | None = None,
) -> Callable[[type], type]:
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `router` | `APIRouter` | — | The FastAPI router to register routes on |
| `base_path` | `str` | — | URL prefix for all endpoints, e.g. `"/items"` |
| `lifecycle` | `str` | `"singleton"` | Instance lifecycle — see below |
| `pk_field_name` | `str \| None` | `None` | Name of the PK field; when set, the field is stripped from the request body on `POST` (create) |

## Usage

### As a decorator

```python
from fastapi import APIRouter
from fastapi_viewsets.decorators.route_viewset import route_viewset

router = APIRouter()

@route_viewset(router, base_path="/items", pk_field_name="id")
class ItemViewSet(ViewSetMixin[int, Item]):
    ...
```

### As a function call

```python
route_viewset(router, base_path="/items", pk_field_name="id")(ItemViewSet)
```

Both forms are equivalent.

## Lifecycle modes

| Mode | Behaviour |
|------|-----------|
| `"singleton"` | One viewset instance is created when the decorator runs and reused for every request. Suitable for stateless viewsets or those that hold shared state (e.g. an in-memory collection). |
| `"per_request"` | A new viewset instance is created for every incoming request. Useful when the viewset needs per-request context (e.g. the current user). |

## Automatic OpenAPI tags

The decorator derives an OpenAPI tag from the class name by stripping the `ViewSet` suffix:

- `ItemViewSet` → tag `Item`
- `UserProfileViewSet` → tag `UserProfile`

## Route ordering

Routes are sorted so that the OpenAPI schema always lists them in a consistent order:

1. `/items` (list / create)
2. `/items/{pk}` (retrieve / update / destroy)
3. `/items/bulk` (bulk operations)
4. `/items/lookup` (lookup)

## pk_field_name and request body

When `pk_field_name` is set, the decorator automatically removes that field from the Pydantic model used as the `POST` request body. This means clients do not need to send the PK when creating a new record — the server assigns it.

```python
@route_viewset(router, base_path="/items", pk_field_name="id")
class ItemViewSet(CollectionViewSet[int, Item], ViewSetMixin[int, Item]):
    def __init__(self):
        super().__init__(container=items_db, pk_field="id")
```

With `pk_field_name="id"`, the `POST /items` body will be `{"name": "...", "price": ...}` instead of `{"id": ..., "name": "...", "price": ...}`.
