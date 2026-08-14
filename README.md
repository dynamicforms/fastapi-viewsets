# DynamicForms FastAPI Viewsets

Django REST Framework-style viewsets for [FastAPI](https://fastapi.tiangolo.com/), with optional
Celery-backed async execution and a matching Vue/TypeScript client counterpart.

- **Python mixins for FastAPI** — compose CRUD and bulk endpoints from small, focused mixin classes.
- **`route_viewset` decorator** — register a viewset on a FastAPI router with a single decorator call.
  Handles type resolution, lifecycle management and OpenAPI schema automatically.
- **`CollectionViewSet`** — zero-boilerplate in-memory viewset backed by any Python list, set or dict.
  Great for prototyping and testing.
- **`celery_viewset` decorator** — move a viewset's execution to a Celery worker with no code changes
  to the viewset itself, for long-running or background processing scenarios (requires the `celery`
  extra).
- **Bulk operations** — first-class support for bulk create, update, partial update and destroy.
- **muxws transport** — reach the same viewsets over a single WebSocket instead of one HTTP request
  per call, dispatched through the same FastAPI app so validation, middleware and response models
  behave identically. In the demo, a burst of 100 requests takes 156 ms over REST and 37 ms over
  muxws.
- **Three list shapes** — a bare array, offset paging, or cursor paging. A viewset declares which,
  and may let a client pick per request with an `X-List-Shape` header.
- **Declarative filters** — declare which fields accept which operators and get query parameters,
  an OpenAPI schema and filtering for free; backends translate what they can into their own query.
- **Vue / TypeScript counterpart** — mirror mixin classes and a `route_rest` factory give you a fully
  typed HTTP client that matches your backend viewset exactly (published separately as
  [`@dynamicforms/fastapi-viewsets`](https://www.npmjs.com/package/@dynamicforms/fastapi-viewsets) on npm).

## Installation

```bash
pip install dynamicforms-fastapi-viewsets

# with Celery-backed viewset support
pip install "dynamicforms-fastapi-viewsets[celery]"

# with the muxws WebSocket transport
pip install "dynamicforms-fastapi-viewsets[muxws]"
```

Requires Python 3.10+, FastAPI and Pydantic v2.

## Quick example

```python
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from fastapi_viewsets.collection_viewset import CollectionViewSet
from fastapi_viewsets.decorators.route_viewset import route_viewset
from fastapi_viewsets.mixins import BulkViewSetMixin


class Item(BaseModel):
    id: int
    name: str


database: dict[int, Item] = {1: Item(id=1, name="First element")}

app = FastAPI()
router = APIRouter()


@route_viewset(router, base_path="/items", pk_field_name="id")
class ItemViewSet(CollectionViewSet[int, Item], BulkViewSetMixin[int, Item]):
    def __init__(self):
        super().__init__(container=database, pk_field="id")


app.include_router(router)
```

See the [full documentation](https://docs.velis.si/dynamicforms/fastapi-viewsets/) for guides on
the mixin system, `route_viewset`, `CollectionViewSet`, `celery_viewset`, the list pipeline and
pagination, the muxws transport, and the Vue client.

## Demo

```bash
python demo.py                       # backend on :8000, frontend on :5173
python demo.py --celery              # ... with every viewset call routed through a Celery worker
npm run test:e2e                     # drives the demo in a browser
```

An infinite-scrolling grid over a 5000-track library, cursor-paged. Switch the whole grid between
the REST and muxws transports and between the in-memory and SQLite backends, and compare their
latency side by side. Sorting and filtering are server-side.

`--celery` needs Redis on localhost:6379. The end-to-end suite starts its own backend and dev
server on their own ports.

## License

MIT — see [LICENSE](LICENSE).
