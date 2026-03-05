# route_viewset — API Reference

```python
from fastapi_viewsets.decorators.route_viewset import route_viewset
```

## Signature

```python
def route_viewset(
    router: APIRouter,
    base_path: str,
    lifecycle: Literal["singleton", "per_request"] = "singleton",
    pk_field_name: str | None = None,
) -> Callable[[type[T]], type[T]]
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `router` | `APIRouter` | — | FastAPI router to register routes on |
| `base_path` | `str` | — | URL prefix for all endpoints |
| `lifecycle` | `"singleton" \| "per_request"` | `"singleton"` | Viewset instance lifecycle |
| `pk_field_name` | `str \| None` | `None` | PK field name; strips it from `POST` request body |

## Return value

Returns the original class unchanged (after registering its routes). Can be used as a decorator or called directly.

## Behaviour

- Collects `__router` from every class in the MRO.
- Resolves all generic `TypeVar` parameters using the concrete types declared on the viewset class.
- Deduplicates routes (same method + path wins once).
- Sorts routes: `""` → `{pk}` → `bulk` → `lookup`, then by HTTP method.
- Derives an OpenAPI tag from the class name (strips `ViewSet` suffix).
- Stores metadata on the class as `cls.__viewset_metadata__`.

## Lifecycle modes

| Value | Instance creation |
|-------|------------------|
| `"singleton"` | Once, at decoration time |
| `"per_request"` | Once per incoming HTTP request |
