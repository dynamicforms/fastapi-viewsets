# CollectionViewSet — API Reference

```python
from fastapi_viewsets.collection_viewset import CollectionViewSet
```

## Class hierarchy

```
AsyncCollectionViewSet[K, T]  (ImplMixin[K, T], ABC)
└── CollectionViewSet[K, T]
```

## AsyncCollectionViewSet

Abstract base. Subclasses must implement the five `container_*` methods.

```python
class AsyncCollectionViewSet(Generic[K, T], ImplMixin[K, T], ABC):
    async def container_list(self) -> list[T]: ...
    async def container_retrieve(self, pk: K) -> T: ...
    async def container_add(self, data: T) -> T: ...
    async def container_update(self, pk: K, data: T, partial: bool) -> T: ...
    async def container_delete(self, pk: K) -> None: ...
```

All `perform_*` methods from `ImplMixin` are implemented here by delegating to the `container_*` methods.

## CollectionViewSet

Concrete implementation backed by a Python collection.

### Constructor

```python
CollectionViewSet(container, pk_field="id")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `container` | `Iterable[T] \| MutableSequence[T] \| MutableSet[T] \| MutableMapping[K, T]` | — | Backing collection |
| `pk_field` | `str` | `"id"` | Attribute/key name used as primary key |

### Supported container types

| Type | Read | Write |
|------|------|-------|
| `Iterable` (non-mutable) | ✅ | ❌ |
| `MutableSequence` (e.g. `list`) | ✅ | ✅ |
| `MutableSet` (e.g. `set`) | ✅ | ✅ |
| `MutableMapping` (e.g. `dict`) | ✅ | ✅ |

### Auto-increment PK

Fields with `json_schema_extra={"autoinc_int": True}` are automatically assigned the next integer value on create when the field is `None` or `0`.

```python
class Item(BaseModel):
    id: int | None = Field(default=None, json_schema_extra={"autoinc_int": True})
    name: str
```

### Errors

Raises `NotFoundError(pk)` (HTTP 404) when a record is not found during retrieve, update, or delete.
