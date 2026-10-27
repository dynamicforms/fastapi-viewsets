"""
Per-viewset OpenAPI documentation for endpoints a mixin provides, whose own docstrings are shared by
every viewset. `@endpoint_docs` keys are action names, as in `@action_configuration`; the viewset's own
docstring becomes its tag description (see docs/guide/endpoint-docs.md).
"""

import inspect

from typing import Any, TypedDict

DOCS_ATTRIBUTE = "__endpoint_docs__"


class EndpointDoc(TypedDict, total=False):
    """OpenAPI fields for one endpoint; a key not given keeps the mixin's own value."""

    summary: str
    description: str
    response_description: str
    deprecated: bool
    tags: list[str]


def endpoint_docs(docs: dict[str, EndpointDoc]):
    """
    Attach per-action documentation to a viewset. Must go below `@route_viewset`, which reads it while
    building routes; the other order raises `ValueError`.
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
    """The documentation of one action, merged along the MRO (subclass entries override)."""
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
    The viewset's own docstring, cleaned. Read from `cls.__dict__` so a mixin's inherited docstring
    never becomes the group description.
    """
    own = cls.__dict__.get("__doc__")
    return inspect.cleandoc(own).strip() if own and own.strip() else None


def register_tag(tag: str, description: str | None) -> None:
    """Record a viewset's description for its tag; `apply_viewset_tags` puts it into the schema root."""
    if description:
        _tag_descriptions[tag] = description


def viewset_tags() -> list[dict[str, str]]:
    """Every registered (tag, description) pair, in the OpenAPI `tags` shape."""
    return [{"name": name, "description": text} for name, text in _tag_descriptions.items()]


def apply_viewset_tags(app: Any, extra: list[dict[str, str]] | None = None) -> Any:
    """
    Set `app.openapi_tags` from the viewsets' descriptions; call once, after the viewsets are decorated.
    `extra` adds the application's own tags and overrides a viewset's description of the same name.
    """
    by_name = {tag["name"]: tag for tag in viewset_tags()}
    for tag in extra or []:
        by_name[tag["name"]] = tag
    app.openapi_tags = list(by_name.values())
    app.openapi_schema = None  # a schema generated earlier lacks the descriptions
    return app
