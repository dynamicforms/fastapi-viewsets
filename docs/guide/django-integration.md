# Django Integration

This library has no dependency on Django and does not manage Django's lifecycle for you. Using it
alongside a Django ORM / Django-based project means the host app is responsible for initializing
Django correctly in **both** the FastAPI process and the Celery worker process, and for a couple
of Celery-specific conventions that only come up once Django apps are involved. Everything below
is a convention on top of the library, except [Django's `ValidationError`](#django-s-validationerror):
the library handles it when Django is installed.

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

Setup a command middleware needs in each worker process (a client it creates, a connection it
opens) goes in
[`Middleware.executor_started()`](./command-middleware#setting-up-the-worker-middleware-executor-started),
which runs in every pool type before the first execution on each of the process's event loops, rather than in a handler of
Celery's `worker_process_init`, which only the `prefork` and `solo` pools send.

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

`DjangoORMViewSet` (`fastapi_viewsets.backends.django_orm`, the `django` extra:
`pip install "dynamicforms-fastapi-viewsets[django]"`) backs a viewset with a Django model and
implements every `perform_*` with the ORM's async API; see [Backends](./list-pipeline#backends).

A hand-written `perform_*` that reaches the ORM keeps business logic in plain **synchronous**
functions that call the Django ORM normally, and calls them via
`asgiref.sync.sync_to_async(fn, thread_sensitive=True)` — the same pattern the library's own
`DjangoSessionAuthBackend` uses internally to resolve a session:

```python
from asgiref.sync import sync_to_async
from myapp.models import Item as DjangoItem

from fastapi_viewsets.context import Context
from fastapi_viewsets.mixins import ListMixin

def _list_items_sync() -> list[dict]:
    return list(DjangoItem.objects.values())

class ItemViewSet(ListMixin[Item]):
    async def perform_list(self, context: Context) -> list[Item]:
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

## Django's `ValidationError`

When Django is installed, a `django.core.exceptions.ValidationError` raised during a request
returns 422 `fastapi-viewsets.validation_failed` with `field_errors`, the same as a pydantic
validation error; so does adorm's `adorm.ValidationError` when adorm is installed (see
[adorm's errors](./adorm-integration#errors)). This applies to:

- every viewset route;
- the application's routes under
  [`install_error_handlers`](./error-codes#the-other-routes-install-error-handlers);
- requests over muxws;
- `celery_viewset` actions executed in a worker.

`error_status()` returns 422 for it and `error_body()` builds the 422 body. It is not logged as a
server error.

```python
from django.core.exceptions import ValidationError

raise ValidationError({"title": ValidationError("Title is taken", code="unique", params={"unique_check": ("title",)})})
```

```json
{
  "detail": "The request failed validation",
  "detail_code": "fastapi-viewsets.validation_failed",
  "detail_params": {},
  "field_errors": {
    "title": [{ "code": "fastapi-viewsets.unique", "params": { "fields": ["title"] }, "detail": "Title is taken" }]
  }
}
```

- The viewset's [`error_field`](./error-codes#field-names) receives the Django field name and returns the
  key; the default returns the field name. A non-field error (`NON_FIELD_ERRORS`) and an error without
  fields are passed as `''`.
- `DjangoORMViewSet` writes each schema field to the model field of the same name, so the key of a written
  field is its schema field name. A model field that is not in the schema keeps its Django name; the
  [frontend](./error-codes#field-errors) puts the errors of a name the form does not hold on the form
  itself. An action that saves another model reports that model's field names, and one of them can be a
  schema field name with another meaning.
- `error_field` receives the Django code as `django.<code>` (`django.unique`). In a bulk action, an error
  raised by a `perform_*` method or an action has no row's key; `DjangoORMViewSet`'s bulk update puts the
  row's primary key in front.
- `detail` is the message with its params substituted, in Django's active language
  (`settings.LANGUAGE_CODE` unless the application activates another one).
- Django codes become [field error codes](./error-codes#field-error-codes) as follows:

  | Django code | `code` | Params |
  |-------------|--------|--------|
  | `required`, `blank`, `null` | `fastapi-viewsets.required` | — |
  | `max_length` | `fastapi-viewsets.max_length` | `maxLength` from `limit_value` (validators) or `max` (form `FileField`) |
  | `min_length` | `fastapi-viewsets.min_length` | `minLength` from `limit_value` |
  | `max_value` / `min_value` | `fastapi-viewsets.max_value` / `fastapi-viewsets.min_value` | `maxValue` / `minValue` from `limit_value`; a date or time as an ISO string |
  | `step_size` without an `offset` | `fastapi-viewsets.multiple_of` | `multipleOf` from `limit_value` |
  | `max_digits`, `max_decimal_places`, `max_whole_digits` | the code of the same name | `maxDigits`, `maxDecimalPlaces`, `maxWholeDigits` from `max` |
  | `invalid_choice` | `fastapi-viewsets.invalid_choice` | — |
  | `invalid_date`, `invalid_time`, `invalid_datetime` | the code of the same name | — |
  | `unique`, `unique_together` | `fastapi-viewsets.unique` | `fields` from `unique_check` |
  | `invalid` | `fastapi-viewsets.invalid` | — |

- An error without a code, e.g. a constraint error whose `violation_error_code` is None, becomes
  `fastapi-viewsets.invalid`.
- A code from the table becomes `django.<code>` when its params do not contain the key named in the
  Params column.
- A `UniqueConstraint` raises the Django code `unique` or `unique_together` only when it has `fields`,
  no `condition` and the default message. In every other case its Django code is the constraint's
  `violation_error_code`, and the rules in this list convert it.
- A code with a namespace (`app.taken`) is unchanged. Any other code becomes `django.<code>`, e.g.
  `django.unique_for_date` or `django.step_size` with an offset.
- For a namespaced code and for `django.<code>`, `params` is Django's params, converted as follows:
  lazy text becomes a string; dates, times, durations, decimals and UUIDs become JSON values; other
  objects, such as the model instance and class in a `unique` error, are left out.

`DjangoORMViewSet` updates catch a `ValidationError` raised by `save()`, e.g. by a model whose
`save()` calls `full_clean()`. A bulk update saves each row in its own savepoint and writes no row
if any row fails. The errors of all failing rows are reported under `<pk>.<field>` (`<pk>` for a
non-field error). In `bulk_destroy`, a `ValidationError` raised while deleting one key becomes that
key's `ApiErrorBody`; the other keys are deleted.

## Combining `celery_viewset` + `route_viewset`

Nothing Django-specific here — see [Combining both decorators](./routers#combining-both-decorators)
for the two valid patterns (subclassing vs. stacking on the same class) and, if stacking, the
required decorator order.

## Multiple `celery_viewset` prefixes

If more than one Django-model-backed `celery_viewset` class is registered (a common shape — one per
Django app/model), make sure every `task_prefix` in use has its own `start_result_reader` call in
the FastAPI `lifespan` — see the [Result passing](./routers#result-passing) section for the
multi-prefix setup and why a single missed prefix makes its calls time out.
