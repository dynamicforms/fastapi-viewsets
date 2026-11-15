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

Each entry is an `EndpointDoc` (a `TypedDict`, `fastapi_viewsets.endpoint_docs`); every field is optional and anything
omitted is left alone:

| field | |
|---|---|
| `summary` | one line, shown in the operation list |
| `description` | markdown, shown when the operation is expanded |
| `response_description` | describes the 200 rather than the request |
| `deprecated` | strikes the operation through |
| `tags` | moves it to a different group |
| `parameters` | descriptions of path, query and header parameters, by their name in the schema (`pk`, `sort`, `limit`, `X-List-Shape`, …) |
| `responses` | `responses` entries by status, merged into the endpoint's entries of the same status (see [Error responses](#error-responses)) |
| `openapi_extra` | fields merged into the operation, e.g. `security` or full parameter objects under `parameters` |

A key outside these raises `ValueError`.

```python
@endpoint_docs({
    "retrieve": {"parameters": {"pk": "Catalogue number of the track."}},
    "list_items": {"parameters": {"sort": "Order of the tracks; `title:asc` by default."}},
})
```

A name in `parameters` that the endpoint does not have raises. A parameter is named the same whether it
is declared `Annotated[int, Path()]` or `int = Path()`. The fields of a filter model are not
endpoint parameters: their descriptions come from the model's `Field(description=...)`.

### Every action: `'*'`

The entry under the key `'*'` (`EVERY_ACTION` in `fastapi_viewsets.endpoint_docs`) documents every action of the
viewset except `schema`:

```python
from fastapi_viewsets.exceptions import SessionExpiredError
from fastapi_viewsets.response_classes import error_entry

@endpoint_docs({
    "*": {
        "openapi_extra": {"security": [{"session": []}]},
        "responses": error_entry(401, "No session: `fastapi-viewsets.session_expired`.", SessionExpiredError()),
    },
    "lookup": {"openapi_extra": {"security": []}},
})
```

A name in its `parameters` that an endpoint does not have is skipped for that endpoint.

### Inheritance and merging

Documentation is inherited through the MRO, so a base viewset can document the actions its
subclasses share and each subclass override only what it disagrees with. The entries of an action are
applied from the base class down; within one class, `'*'` comes before the action's own entry. A later
entry replaces a field, except:

| field | merged |
|---|---|
| `parameters` | per parameter |
| `responses` | per status with `merge_responses()`, starting from the endpoint's entry of that status (the mixin's, the package's) |
| `openapi_extra` | `parameters` lists concatenated; dicts merged per key, recursively; any other value replaced (`security`, …): `merge_openapi_extra(base, extra)` |

The merged `openapi_extra` goes into the route's own (the request body description, `x-viewset-action`), and the
parameters of the [command middleware](./command-middleware) follow its `parameters`. A list declared in
`openapi_extra`, other than the top-level `parameters`, replaces the operation's list of the same key: a declared
`security` replaces the one of `settings.viewsets_security_scheme`, and `security: []` documents a public endpoint.

The action name of the `/schema` endpoint is `schema`; `/schema` carries the same operations as the application's
OpenAPI document.

### Viewsets the package routes

`register_events()` and taskwire's `register_taskwire_rest()` route a viewset class themselves, so
`@endpoint_docs` cannot go below `@route_viewset` on it. Call `endpoint_docs` on that class before the
register call:

```python
from fastapi_viewsets.endpoint_docs import endpoint_docs
from fastapi_viewsets.events.viewset import EventsViewSet, register_events

endpoint_docs({"*": {"openapi_extra": {"security": [{"session": []}]}}})(EventsViewSet)
register_events(router)
```

`register_events()` routes a subclass of `EventsViewSet`, which inherits the documentation;
`register_taskwire_rest()` routes `TaskwireViewSet` itself. The documentation applies to every mount
of that class.

## Parameters and library models

Every parameter the package declares has a default description:

| parameter | description |
|---|---|
| `pk` | the `Field(description=...)` of the model's primary key field, else `Primary key of the record.` |
| `sort` | the ordering syntax (`SORT_DESCRIPTION`) |
| `offset` | `OFFSET_DESCRIPTION` |
| `limit` | the page size, with the viewset's `default_page_size` and `max_page_size` |
| `cursor` | `CURSOR_DESCRIPTION` |
| `X-List-Shape` | the shapes and the default |

A `pk` the endpoint declares with `Path(description=...)` (either form) keeps that description.

The request bodies the mixins derive from the viewset's model, `<Model>NoPK` (create) and
`<Model>Partial` (`PATCH`), carry the model's docstring as their description.

A list without a filter model has no filter parameter. Every field of a model the package publishes
(`PaginatedList`, `CursorPage`, `LookupItem`, `LookupFilter`, `ApiErrorBody`, the events models) has
a description, and `/schema` answers the named schema `OpenAPIDocument`
(`fastapi_viewsets.routing`).

## Across the whole schema: post-processing

The names of the tag groups come from `settings.viewsets_tag_name`; see
[Automatic OpenAPI tags](./routers#automatic-openapi-tags). The setting names a viewset's group
everywhere it appears: the operations, the tag description, `/schema` and the muxws registration.

For edits that apply to the document as a whole — adding the same error responses everywhere,
appending the same link to every operation — replace `app.openapi`:

```python
from fastapi.openapi.utils import get_openapi

def custom_openapi():
    if not app.openapi_schema:
        schema = get_openapi(
            title=app.title, version=app.version, routes=app.routes, tags=app.openapi_tags
        )
        for item in schema["paths"].values():
            for operation in item.values():
                operation["description"] = (
                    operation.get("description", "") + "\n\nEverything here needs a session."
                )
        app.openapi_schema = schema
    return app.openapi_schema

app.openapi = custom_openapi
```

The demo uses this to add a **Try it in Swagger UI** button to every operation, so that ReDoc — which
cannot execute requests — links to the page that can. See `demo/backend/openapi_docs.py`.

## Which to use

| scope | mechanism |
|---|---|
| what a group of endpoints is called | `settings.viewsets_tag_name` |
| what a group of endpoints is | the viewset's docstring |
| what one endpoint does | `@endpoint_docs` |
| the document as a whole | post-processing `app.openapi` |

They compose; using all of them is normal.

## Response examples

A list endpoint that offers several response shapes documents each of them automatically, with the
example named after the `X-List-Shape` value that produces it — see
[the list pipeline](./list-pipeline.md#declaring-a-shape). Nothing to write.

## Error responses

Every operation of a viewset documents its error responses with the named schema `ApiErrorBody`, the
[error body](./error-codes#the-error-body), and `ApiFieldError`, one entry of its `field_errors`; every field of both
has a description. Nothing to write:

| status | when | examples |
|---|---|---|
| `404` | the operations that address an item by `pk` (`retrieve`, `update`, `partial_update`, `destroy`) and by the keys of the body (`bulk_update`, `bulk_partial_update`): `fastapi-viewsets.not_found` | `not_found` |
| `422` | the request failed validation, `fastapi-viewsets.validation_failed` with `field_errors`; with the `Idempotency` middleware `fastapi-viewsets.idempotency_key_reused`; on the events `subscribe` `fastapi-viewsets.event_topic_invalid`, on `poll` `fastapi-viewsets.event_cursors_exceeded` | `validation_failed`, `idempotency_key_reused`, and the endpoint's own code |
| `500` | an exception without a handler, `fastapi-viewsets.unhandled_error`; a deferred action that failed, `fastapi-viewsets.action_failed` | `unhandled_error` |
| `default` | any other error: the package's codes by status, each with the middleware or collection that answers it; on the list endpoint of a viewset serving the cursor shape the 400 cursor codes; `fastapi-viewsets.http_error` for an `HTTPException` without a code | `http_error`, `rate_limited`, with the cursor codes `cursor_stale` |

The entries replace FastAPI's own `422` with `HTTPValidationError`, which no longer appears in the document. An
entry the endpoint declares itself (`@router.get(..., responses={409: {...}})`) is kept, and replaces the package's
entry of the same status. An entry of `@endpoint_docs` `responses` is merged with the entry of the same status, so
the package's examples stay. The entries are in `fastapi_viewsets.response_classes`: `VIEWSET_ERROR_RESPONSES` on every
endpoint, and the endpoints' own `NOT_FOUND_RESPONSE`, `BULK_NOT_FOUND_RESPONSE`, `CURSOR_ERROR_RESPONSES`,
`SUBSCRIBE_ERROR_RESPONSES` and `POLL_ERROR_RESPONSES`.

### An application's own error responses

`error_entry(status, description, *errors, as_ref=False, **named_errors)` builds one entry, `{str(status): entry}`,
whose examples are the bodies its exceptions are answered with (`error_body()`):

- a positional exception gives an example named after the last segment of its code, summarised by its `detail`;
- a keyword exception gives an example named and summarised by its keyword, for errors the code does not tell apart
  (two `fastapi-viewsets.http_error`, two `app.conflict` with different `params`).

Two examples of one name raise `ValueError`. The body is `model: ApiErrorBody`, the form of route declarations; with
`as_ref=True` it is `content.application/json.schema: {"$ref": "#/components/schemas/ApiErrorBody"}`, the form for
editing a generated document.

`merge_responses(*responses)` merges entries by status, in either form, and returns a new dict with string keys:

| part | merged |
|---|---|
| `description` | joined as paragraphs (`\n\n`), each paragraph once |
| `content` | per media type; a media type of one entry only is kept as it is |
| `examples` | by name, a later example replaces one of the same name; a lone `example` becomes a named one (`example`, `example_2`, …), unless an equal example is there |
| `headers` | by name, a later header replaces one of the same name |
| any other key (`model`, `schema`, `links`) | a later entry's replaces |

```python
from fastapi import APIRouter, HTTPException

from fastapi_viewsets.response_classes import NOT_FOUND_RESPONSE, error_entry, merge_responses

class OrderViewSet(CollectionViewSet[int, Order]):
    __router = APIRouter()

    @__router.get(
        "{pk}/invoice",
        responses=merge_responses(
            NOT_FOUND_RESPONSE,
            error_entry(404, "The order has no invoice.", no_invoice=HTTPException(404, "No invoice")),
            error_entry(409, "The invoice is being issued: `app.invoice_pending`.", InvoicePendingError(42)),
        ),
    )
    async def invoice(self, pk: int) -> Invoice: ...
```

`with_error_responses(responses)` puts `responses` over `VIEWSET_ERROR_RESPONSES`, an entry replacing the package's
entry of the same status.
