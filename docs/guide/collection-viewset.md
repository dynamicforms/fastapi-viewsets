# CollectionViewSet

`CollectionViewSet` is a ready-made `ImplMixin` implementation that stores data in any Python in-memory collection. It is ideal for prototyping, demos, and tests — no database required.

## Supported container types

| Container type | Supported operations |
|----------------|---------------------|
| `list` / `MutableSequence` | list, retrieve, create, update, destroy |
| `set` / `MutableSet` | list, retrieve, create, update, destroy |
| `dict` / `MutableMapping` | list, retrieve, create, update, destroy |
| Any `Iterable` (read-only) | list, retrieve only |

## Basic usage

```python
from pydantic import BaseModel
from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.mixins import BulkViewSetMixin
from fastapi_viewsets.decorators.route_viewset import route_viewset
from fastapi import APIRouter

class Item(BaseModel):
    id: int | None = None
    name: str
    price: float

items: list[Item] = []
router = APIRouter()

@route_viewset(router, base_path="/items", pk_field_name="id")
class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item]):
    def __init__(self):
        super().__init__(container=items, pk_field="id")
```

That's it — all CRUD and bulk endpoints are available with no further code.

## Constructor parameters

```python
CollectionViewSet(container, pk_field="id")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `container` | collection | — | The backing Python collection |
| `pk_field` | `str` | `"id"` | Name of the primary key attribute/key on each item |

## Auto-increment PK

If your Pydantic model has a field annotated with `json_schema_extra={"autoinc_int": True}`, `CollectionViewSet` will automatically assign the next integer value when creating a new record and the field is `None` or `0`.

```python
class Item(BaseModel):
    id: int | None = Field(default=None, json_schema_extra={"autoinc_int": True})
    name: str
    price: float
```

## Using a dict container

When using a `dict`, the key is the PK and the value is the item. `CollectionViewSet` handles the mapping transparently:

```python
items: dict[int, Item] = {}

@route_viewset(router, base_path="/items", pk_field_name="id")
class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item]):
    def __init__(self):
        super().__init__(container=items, pk_field="id")
```

## Extending with custom logic

You can override any `container_*` method to add custom behaviour (e.g. validation, side effects):

```python
class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item]):
    def __init__(self):
        super().__init__(container=items, pk_field="id")

    async def container_add(self, data: Item) -> Item:
        # custom pre-processing
        data.name = data.name.strip()
        return await super().container_add(data)
```
