# Documenting endpoints

Most of a viewset's endpoints come from mixins, so their docstrings — the only thing OpenAPI has to
describe them — are the same for every viewset in an application. Three mechanisms replace that
text, at three scopes.

## Per viewset: its own docstring

A viewset's endpoints are grouped under one tag, and that group's description is the intro a reader
sees before any of them. It comes from the viewset's own docstring:

```python
@route_viewset(router, base_path="/music", pk_field_name="id")
class MusicTrackViewSet(CollectionViewSet[int, MusicTrack], CursorListMixin[MusicTrack]):
    """
    The music library, held in memory.

    Listing is cursor-paged: send `X-List-Shape: plain` for a bare array instead. Filtering and
    sorting are declared rather than written - see the `year__gte` and `title__icontains`
    parameters, neither of which has any code behind it.
    """
```

Markdown, rendered by ReDoc as the section intro. Only the viewset's own docstring counts; one
inherited from a mixin is ignored, so a viewset that documents nothing has no group description.

Tag descriptions live at the root of an OpenAPI document, so they are applied to the application
once, after the viewsets are decorated:

```python
from fastapi_viewsets.endpoint_docs import apply_viewset_tags

app.include_router(router)
apply_viewset_tags(app)
```

Pass `extra=[{"name": ..., "description": ...}]` for groups the application owns itself; anything
it defines overrides the viewset's docstring.

::: warning If you replace `app.openapi`
`get_openapi(...)` does not read `app.openapi_tags` on its own. Pass `tags=app.openapi_tags`
explicitly or the descriptions vanish without a word.
:::

## Per endpoint: `@endpoint_docs`

```python
from fastapi_viewsets.endpoint_docs import endpoint_docs

@route_viewset(router, base_path="/music", pk_field_name="id")
@endpoint_docs({
    "list_items": {
        "summary": "Browse the library",
        "description": "Cursor-paged; follow `next` to walk forward.",
    },
    "retrieve": {"summary": "One track by id"},
    "create":   {"summary": "Add a track"},
    "count":    {"summary": "How many tracks there are"},
})
class MusicTrackViewSet(CollectionViewSet[int, MusicTrack], BulkViewSetMixin[int, MusicTrack]):
    ...
```

**It goes below `@route_viewset`**, which reads it while building the routes. The other order
raises.

Keys are action names: the mixin action (`list_items`, `bulk_create`, `partial_update`, …) or a
custom endpoint's own method name — the same names `@action_configuration` uses. A name that
matches no endpoint raises.

Every field is optional and anything omitted is left alone:

| field | |
|---|---|
| `summary` | one line, shown in the operation list |
| `description` | markdown, shown when the operation is expanded |
| `response_description` | describes the 200 rather than the request |
| `deprecated` | strikes the operation through |
| `tags` | moves it to a different group |

Documentation is inherited through the MRO, so a base viewset can document the actions its
subclasses share and each subclass override only what it disagrees with.

## Across the whole schema: post-processing

For edits that apply to the document as a whole — renaming tag groups, adding the same error
responses everywhere, appending the same link to every operation — replace `app.openapi`:

```python
from fastapi.openapi.utils import get_openapi

TAG_DESCRIPTIONS = {"MusicTrack": "The catalogue. Everything here needs a session."}

def custom_openapi():
    if not app.openapi_schema:
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema["tags"] = [
            {"name": name, "description": description}
            for name, description in TAG_DESCRIPTIONS.items()
        ]
        app.openapi_schema = schema
    return app.openapi_schema

app.openapi = custom_openapi
```

The demo uses this to add a **Try it in Swagger UI** button to every operation, so that ReDoc — which
cannot execute requests — links to the page that can. See `demo/backend/openapi_docs.py`.

## Which to use

| scope | mechanism |
|---|---|
| what a group of endpoints is | the viewset's docstring |
| what one endpoint does | `@endpoint_docs` |
| the document as a whole | post-processing `app.openapi` |

They compose; using all three is normal.

## Response examples

A list endpoint that offers several response shapes documents each of them automatically, with the
example named after the `X-List-Shape` value that produces it — see
[the list pipeline](./list-pipeline.md#declaring-a-shape). Nothing to write.
