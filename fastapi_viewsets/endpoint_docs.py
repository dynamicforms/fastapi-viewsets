"""
Per-viewset documentation for endpoints a mixin provided.

`list_items`, `create`, `retrieve` and the rest are shared library code. Their docstrings are the
only thing OpenAPI has to describe them, and that docstring is identical for every viewset in an
application - so a generated API reference says "List a queryset" eleven times and explains
nothing. There is no docstring to attach per viewset, because there is no per-viewset method.

This attaches one:

    @endpoint_docs({
        "list_items": {
            "summary": "Browse the catalogue",
            "description": "Cursor-paged; follow `next` to walk forward.",
        },
        "create": {"summary": "Add a track"},
        "count":  {"summary": "How many tracks there are"},
    })
    @route_viewset(router, base_path="/music", pk_field_name="id")
    class MusicTrackViewSet(...):
        ...

Keys are action names - the same ones `@action_configuration` uses, so there is no second
vocabulary to learn: the mixin action (`list_items`, `bulk_create`, …), or the name of a custom
endpoint's own method.

The alternative is to post-process the finished OpenAPI schema, which is the right tool when the
edits are not per-endpoint at all - renaming tag groups, adding a section intro, appending the same
link to every operation. The two compose; see docs/guide/endpoint-docs.md.
"""

import inspect

from typing import Any, TypedDict

DOCS_ATTRIBUTE = "__endpoint_docs__"


class EndpointDoc(TypedDict, total=False):
    """
    What can be said about one endpoint. Every key is optional and any not given is left alone, so
    a viewset can supply a summary and leave the description to the mixin's own docstring.
    """

    summary: str
    description: str
    response_description: str
    deprecated: bool
    tags: list[str]


def endpoint_docs(docs: dict[str, EndpointDoc]):
    """
    Attaches per-action documentation to a viewset.

    Goes **below** `route_viewset`, so that it runs first:

        @route_viewset(router, base_path="/music", pk_field_name="id")
        @endpoint_docs({...})
        class MusicTrackViewSet(...): ...

    Decorators apply bottom-up, and `route_viewset` reads this while it builds the routes. Written
    the other way round it would run too late; rather than silently document nothing, that is
    refused with a message saying which way round to put it.
    """

    def decorator(cls):
        if getattr(cls, "__viewset_metadata__", None) is not None:
            raise ValueError(
                f"@endpoint_docs must go below @route_viewset on {cls.__name__} - decorators apply "
                f"bottom-up, and route_viewset reads the documentation while it builds the routes"
            )
        setattr(cls, DOCS_ATTRIBUTE, {**getattr(cls, DOCS_ATTRIBUTE, {}), **docs})
        return cls

    return decorator


def docs_for(cls: type, action: str) -> dict[str, Any]:
    """
    What this viewset says about one action, walking the MRO so a base viewset's documentation is
    inherited and a subclass can override just the entries it disagrees with.
    """
    merged: dict[str, Any] = {}
    for base in reversed(cls.__mro__):
        entry = base.__dict__.get(DOCS_ATTRIBUTE, {}).get(action)
        if entry:
            merged.update(entry)
    return merged


# ---------------------------------------------------------------------------
# Viewset-level description
# ---------------------------------------------------------------------------

_tag_descriptions: dict[str, str] = {}


def viewset_description(cls: type) -> str | None:
    """
    What a viewset says about itself: its own docstring, cleaned of indentation.

    `cls.__dict__` rather than `cls.__doc__`, so a viewset that says nothing stays silent instead
    of inheriting "List a queryset" from a mixin - which would be worse than an empty section,
    because it would look deliberate.
    """
    own = cls.__dict__.get("__doc__")
    return inspect.cleandoc(own).strip() if own and own.strip() else None


def register_tag(tag: str, description: str | None) -> None:
    """
    Records a viewset's description against the tag its endpoints are grouped under.

    OpenAPI keeps tag descriptions at the root of the document rather than on the operations, so
    this cannot be attached while a route is built - it has to be collected and applied to the
    application once, which is what `apply_viewset_tags` does.
    """
    if description:
        _tag_descriptions[tag] = description


def viewset_tags() -> list[dict[str, str]]:
    """Every registered (tag, description) pair, in the OpenAPI `tags` shape."""
    return [{"name": name, "description": text} for name, text in _tag_descriptions.items()]


def apply_viewset_tags(app: Any, extra: list[dict[str, str]] | None = None) -> Any:
    """
    Puts the viewsets' own descriptions into an application's schema.

    Call once, after the viewsets are decorated - which is why this is a function rather than
    something passed to `FastAPI(...)`: the app usually exists before the viewsets do.

        route_viewset(router, base_path="/music", pk_field_name="id")(MusicTrackViewSet)
        app.include_router(router)
        apply_viewset_tags(app)

    `extra` is merged in for tags the application owns itself; anything it defines wins, since a
    viewset's docstring is a default rather than the last word. Groups a viewset never described
    are left out entirely - a tag with no description renders exactly as it did before.
    """
    by_name = {tag["name"]: tag for tag in viewset_tags()}
    for tag in extra or []:
        by_name[tag["name"]] = tag
    app.openapi_tags = list(by_name.values())
    app.openapi_schema = None  # already-generated schemas would not have the descriptions
    return app
