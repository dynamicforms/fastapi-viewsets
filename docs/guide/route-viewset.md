# route_viewset decorator

`route_viewset` is the glue between your viewset class and a FastAPI router. It collects all routes defined by the mixin hierarchy, resolves generic type parameters, and registers them on the given router.

## Signature

```python
def route_viewset(
    router: APIRouter,
    base_path: str,
    lifecycle: LifecycleType = "singleton",
    pk_field_name: str | None = None,
) -> Callable[[type], type]:
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `router` | `APIRouter` | — | The FastAPI router to register routes on |
| `base_path` | `str` | — | URL prefix for all endpoints, e.g. `"/items"` |
| `lifecycle` | `LifecycleType` | `"singleton"` | Instance lifecycle — see below |
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
| `"singleton"` | One instance is created when the decorator runs and reused for every request. `load_state()` / `save_state()` are called on every request, so state can be shared across multiple processes (e.g. via Redis). Note: there is currently no locking — concurrent requests may cause race conditions when writing state. |
| `"per-request"` | A new instance is created for every incoming request. `load_state()` / `save_state()` are **not** called. Useful when the viewset needs per-request context (e.g. the current user) with no shared state. |
| `"instance-key"` | A new instance is created per request. `load_state()` is called before the endpoint and `save_state()` after it, so state is loaded fresh and persisted on every request. Same race-condition caveat as `"singleton"` applies. |

## State hooks: `load_state` / `save_state`

When the lifecycle is `"singleton"` or `"instance-key"`, the runner looks for two optional async methods on the viewset instance:

| Method | When called | Purpose |
|--------|-------------|---------|
| `async def load_state(self)` | Before the endpoint handler | Restore state from an external store (e.g. read from Redis or a database) |
| `async def save_state(self)` | After the endpoint handler (in a `finally` block) | Persist state back to the external store |

Neither method is required. If a method is absent on the class it is simply skipped.

```python
@route_viewset(router, base_path="/session", lifecycle="instance-key")
class SessionViewSet(ListMixin[str]):
    def __init__(self):
        self.items: list[str] = []

    async def load_state(self):
        self.items = await redis.lrange("session:items", 0, -1)

    async def save_state(self):
        await redis.delete("session:items")
        if self.items:
            await redis.rpush("session:items", *self.items)

    async def perform_list(self) -> list[str]:
        return self.items
```

`save_state` is called even if the endpoint raises an exception, so the store always reflects the last consistent state written by `load_state`.

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
