# Django Integration

This library has no dependency on Django and does not manage Django's lifecycle for you. Using it
alongside a Django ORM / Django-based project means the host app is responsible for initializing
Django correctly in **both** the FastAPI process and the Celery worker process, and for a couple
of Celery-specific conventions that only come up once Django apps are involved. Everything below
is a convention on top of the library, not special-cased library behavior.

---

## Django init in the FastAPI process

The library never calls `django.setup()` — the host FastAPI app must, and it must happen **before**
any Django-dependent import (models, ORM, `django.contrib.auth`, ...), or Django raises
`AppRegistryNotReady`.

A robust pattern: put `DJANGO_SETTINGS_MODULE` + `django.setup()` in their own tiny module with no
other content, and make it the *first* import in the FastAPI entrypoint — even before stdlib
imports — so linters/import-sorters can't reorder it away from the top:

```python
# myproject/django_setup.py
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")
django.setup()
```

```python
# myproject/main.py
import myproject.django_setup  # noqa: F401,I001 - must run first, see module docstring

import asyncio  # everything else, including anything that touches Django models, comes after

from fastapi import FastAPI
...
```

## Django init in the Celery worker process

This is simpler and usually needs no explicit call. If the worker is started as
`celery -A myproject.celery_app worker`, and `celery_app.py` sets `DJANGO_SETTINGS_MODULE` before
constructing the `Celery(...)` instance, Celery's own built-in Django fixup
(`celery.fixups.django`) detects this and calls `django.setup()` automatically via its
`worker_init`/`import_modules` signal handlers. See
[Celery's own documentation](https://docs.celeryq.dev/en/stable/userguide/configuration.html) for
that fixup rather than reimplementing it — nothing on this library's side is involved.

## `autodiscover_tasks` for non-conventional task locations

`celery_viewset`-decorated classes register Celery tasks as a side effect of being
imported/decorated (see the `celery_worker.py` example under [`celery_viewset` → Usage](./routers#celery-viewset),
which does `import myapp.viewsets  # noqa: F401`). Celery's `app.autodiscover_tasks()` (with no
arguments) only looks for `tasks.py` inside each installed Django app by convention. If your
`celery_viewset`-decorated classes don't live in a `tasks.py`, add an **additional**, explicit call
naming the actual module:

```python
app.autodiscover_tasks(packages=["myapp"], related_name="viewsets")  # looks for myapp/viewsets.py
```

A plain top-level `import myapp.viewsets` at Celery app construction time (instead of
`autodiscover_tasks`) does **not** work as a substitute: Django apps aren't loaded yet at that
point, so importing anything that touches Django models raises `AppRegistryNotReady`.
`autodiscover_tasks` defers the import correctly, via Celery's own lazy-loading — that's the reason
it exists, not just a naming convention.

## Django ORM access convention

There is no Django-QuerySet-backed ViewSet/container equivalent to `CollectionViewSet` in this
library — the `django` extra (`pip install "dynamicforms-fastapi-viewsets[django]"`) only wires up
Django-based [authentication backends](./authentication#djangosessionauthbackend-djangosessioncookieauthbackend-real-django-sessions),
nothing ORM-related. ([More backends are planned](./rationale#built-in-implementation-classes), just not shipped yet.)

Until then, the recommended convention: keep business logic in plain **synchronous** functions that
call the Django ORM normally, and call them from `perform_*` (or any `async def` endpoint method)
via `asgiref.sync.sync_to_async(fn, thread_sensitive=True)` — the same pattern the library's own
`DjangoSessionAuthBackend` uses internally to resolve a session:

```python
from asgiref.sync import sync_to_async
from myapp.models import Item as DjangoItem

def _list_items_sync() -> list[dict]:
    return list(DjangoItem.objects.values())

class ItemViewSet(ListMixin[Item]):
    async def perform_list(self) -> list[Item]:
        rows = await sync_to_async(_list_items_sync, thread_sensitive=True)()
        return [Item(**row) for row in rows]
```

**Why this matters even outside Celery:** when an action is `celery_viewset`-dispatched,
`celery_viewset_server` runs `perform_*` inside the Celery worker's own asyncio event loop (see
[`server.py`](./routers#celery-viewset) and the `lifecycle_runner` it calls into). A direct
synchronous Django ORM call made from inside a running event loop trips Django's `async_unsafe`
guard; `sync_to_async(..., thread_sensitive=True)` routes the call to a worker thread instead,
which is safe. This applies whether or not the action actually goes through Celery — a plain
`route_viewset`-only endpoint's `async def perform_*` is itself running inside FastAPI's event
loop, so the same guard applies there too. Use `sync_to_async` as the standard pattern for any ORM
access from `perform_*`, not just as a Celery-specific workaround.

## Combining `celery_viewset` + `route_viewset`

Nothing Django-specific here — see [Combining both decorators](./routers#combining-both-decorators)
for the two valid patterns (subclassing vs. stacking on the same class) and, if stacking, the
required decorator order.

## Multiple `celery_viewset` prefixes

If more than one Django-model-backed `celery_viewset` class is registered (a common shape — one per
Django app/model), make sure every `task_prefix` in use has its own `start_result_reader` call in
the FastAPI `lifespan` — see the [Result passing](./routers#result-passing) section for the
multi-prefix setup and why a single missed prefix causes a silent hang rather than an error.
