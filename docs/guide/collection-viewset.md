# CollectionViewSet

`CollectionViewSet` is a ready-made `ImplMixin` implementation that stores data in any Python in-memory collection. It
is ideal for prototyping, demos, and tests — no database required.

## Supported container types

| Container type | Supported operations |
|----------------|---------------------|
| `list` / `MutableSequence` | list, retrieve, create, update, destroy |
| `set` / `MutableSet` | list, retrieve, create, update, destroy |
| `dict` / `MutableMapping` | list, retrieve, create, update, destroy |
| Any `Iterable` (read-only) | list, retrieve only |

A write to a read-only container raises `ReadOnlyCollectionError`: status 405, code `read_only`.

A partial update (`PATCH`) writes the fields the request stated onto the stored item: into a `dict`,
as attributes of any other object. An item of a set is hashable and is replaced by a copy holding
the new values, so a set holds pydantic models (frozen), named tuples or dataclasses (frozen).

## One copy per process

The container lives in the memory of the process that holds the viewset. Every process that imports
the viewset's module has its own container: each worker process of the ASGI server, and each process
of a Celery worker pool that runs a [`celery_viewset`](./routers#celery-viewset). A write changes the
container of the process that served it; a call served by another process reads its own container
and does not see the write.

With more than one process the application keeps the data consistent itself: it runs one process,
or it keeps the data outside the processes, in a database (`DjangoORMViewSet`) or in a shared store
such as Redis or Valkey behind a container of its own.

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
| `pk_field` | `str` | `"id"` | Name of the primary key attribute/key on each item; also the viewset's `pk_field_name`, which the cursor list shape and `ChangeEvents` read |

## Auto-increment PK

If your Pydantic model has a field annotated with `json_schema_extra={"autoinc_int": True}`, `CollectionViewSet` will
automatically assign the next integer value when creating a new record and the field is `None` or `0`.

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
