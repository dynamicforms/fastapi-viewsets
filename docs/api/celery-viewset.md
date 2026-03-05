# CeleryViewSet — API Reference

```python
from fastapi_viewsets.celery_viewset import CeleryViewSet
```

## Class hierarchy

```
BulkViewSetMixin[K, T]
└── CeleryViewSet[K, T]
```

## Constructor

```python
CeleryViewSet(celery, task_prefix)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `celery` | `Celery` | Configured Celery application instance |
| `task_prefix` | `str` | Prefix for task names (e.g. `"items"`) |

## Task name mapping

| `perform_*` method | Celery task name |
|--------------------|-----------------|
| `perform_list()` | `{prefix}.list` |
| `perform_retrieve(pk)` | `{prefix}.retrieve` |
| `perform_create(data)` | `{prefix}.create` |
| `perform_bulk_create(data)` | `{prefix}.bulk_create` |
| `perform_update(pk, data, partial=False)` | `{prefix}.update` |
| `perform_update(pk, data, partial=True)` | `{prefix}.partial_update` |
| `perform_bulk_update(records, partial=False)` | `{prefix}.bulk_update` |
| `perform_bulk_update(records, partial=True)` | `{prefix}.bulk_partial_update` |
| `perform_destroy(pk)` | `{prefix}.destroy` |
| `perform_bulk_destroy(pk)` | `{prefix}.bulk_destroy` |

## Result reader helpers

```python
from fastapi_viewsets.decorators.celery_viewset import (
    start_result_reader,
    stop_result_reader,
    get_result_queue_key,
)
```

| Function | Description |
|----------|-------------|
| `get_result_queue_key(prefix)` | Returns the Redis key used for result pub/sub |
| `start_result_reader(redis, queue_key)` | Starts the async background task that reads Celery results |
| `stop_result_reader()` | Stops the background result reader |

These should be called in the FastAPI `lifespan` context manager.
