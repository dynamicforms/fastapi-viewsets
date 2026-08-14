# Documenting endpoints

Most of a viewset's endpoints come from mixins. `list_items`, `create`, `retrieve` and the rest are
shared library code, so their docstrings — the only thing OpenAPI has to describe them — are
identical for every viewset in your application. A generated API reference ends up saying "List the
collection" eleven times and explaining nothing, and there is no per-viewset method to attach a
better docstring to.

There are two ways to fix that. They solve different halves of the problem and compose.

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

**It goes below `@route_viewset`.** Decorators apply bottom-up, and `route_viewset` reads the
documentation while it builds the routes — written the other way round it would run too late, so
that order is refused with a message rather than silently documenting nothing.

Keys are action names: the mixin action (`list_items`, `bulk_create`, `partial_update`, …) or a
custom endpoint's own method name. They are the same names `@action_configuration` uses, so there
is no second vocabulary. A name that matches no endpoint raises — a typo would otherwise leave the
endpoint on the mixin's generic docstring, which looks exactly like not having written the entry.

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

Some documentation is not per-endpoint at all — renaming tag groups, giving each group an intro,
adding the same error responses everywhere, appending the same link to every operation. Doing that
one endpoint at a time would mean repeating yourself once per route.

For those, replace `app.openapi`:

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

The demo does exactly this to add a **Try it in Swagger UI** button to every operation, so that
ReDoc — which renders beautifully and cannot execute anything — links to the page that can. See
`demo/backend/openapi_docs.py`; the fiddly parts there are load-bearing and commented.

## Which to use

Use `@endpoint_docs` when the text is *about this viewset's endpoint*. It lives next to the code it
describes, it is checked against the endpoints that exist, and it needs no schema-shaped
vocabulary.

Use post-processing when the edit is *about the schema* — group names, group intros, a rule applied
to every operation. Expressing those per endpoint means writing them once per route and keeping
them in step by hand.

Neither replaces the other and using both is normal: `@endpoint_docs` for what each endpoint means,
a post-processor for what the document as a whole should look like.

## Response examples

A list endpoint that offers several response shapes documents each of them automatically, with the
example named after the `X-List-Shape` value that produces it — see
[the list pipeline](./list-pipeline.md#declaring-a-shape). Nothing to write.
