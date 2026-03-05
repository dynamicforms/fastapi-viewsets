# CeleryViewSet

`CeleryViewSet` is a `BulkViewSetMixin` implementation that delegates every CRUD operation to a Celery task. It is designed for scenarios where the actual data processing happens in a background worker (e.g. long-running computations, external system integrations).

## How it works

Each `perform_*` method sends a Celery task named `<task_prefix>.<action>`. The viewset does not wait for the result synchronously — it returns the Celery `AsyncResult` immediately. A separate result-reader mechanism (based on Redis pub/sub) is provided to stream results back to the HTTP response.

## Basic usage

```python
from celery import Celery
from fastapi_viewsets.celery_viewset import CeleryViewSet
from fastapi_viewsets.decorators.route_viewset import route_viewset
from fastapi import APIRouter

celery_app = Celery("demo", broker="redis://localhost:6379/0")
router = APIRouter()

@route_viewset(router, base_path="/items", pk_field_name="id")
class ItemViewSet(CeleryViewSet[int, Item]):
    def __init__(self):
        super().__init__(celery=celery_app, task_prefix="items")
```

## Task naming convention

For a viewset with `task_prefix="items"`, the following Celery tasks must exist in your worker:

| Action | Task name |
|--------|-----------|
| list | `items.list` |
| retrieve | `items.retrieve` |
| create | `items.create` |
| bulk_create | `items.bulk_create` |
| update (full) | `items.update` |
| partial_update | `items.partial_update` |
| bulk_update | `items.bulk_update` |
| bulk_partial_update | `items.bulk_partial_update` |
| destroy | `items.destroy` |
| bulk_destroy | `items.bulk_destroy` |

## Worker example

```python
# celery_worker.py
from celery import Celery

app = Celery("demo", broker="redis://localhost:6379/0")

@app.task(name="items.list")
def items_list():
    return [{"id": 1, "name": "Widget", "price": 9.99}]

@app.task(name="items.retrieve")
def items_retrieve(pk):
    ...

@app.task(name="items.create")
def items_create(data):
    ...
```

## Result reader

Because Celery tasks are asynchronous, `CeleryViewSet` relies on a result-reader that listens for task results on a Redis queue and forwards them to the waiting HTTP handler.

Start and stop the reader in your FastAPI lifespan:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi_viewsets.decorators.celery_viewset import (
    start_result_reader,
    stop_result_reader,
    get_result_queue_key,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    queue_key = get_result_queue_key("items")
    await start_result_reader(redis_async, queue_key)
    yield
    await stop_result_reader()

app = FastAPI(lifespan=lifespan)
```

## Constructor parameters

```python
CeleryViewSet(celery, task_prefix)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `celery` | `Celery` | Configured Celery application instance |
| `task_prefix` | `str` | Prefix used to build task names (e.g. `"items"`) |
