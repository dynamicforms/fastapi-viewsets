"""
Per-viewset OpenAPI documentation for endpoints a mixin provides, whose own docstrings are shared by
every viewset. `@endpoint_docs` keys are action names, as in `@action_configuration`; the viewset's own
docstring becomes its tag description (see docs/guide/endpoint-docs.md).
"""

import copy
import inspect

from typing import Any, TypedDict

from .response_classes import merge_responses

DOCS_ATTRIBUTE = "__endpoint_docs__"

EVERY_ACTION = "*"
"""The `@endpoint_docs` key whose entry applies to every action of the viewset except `schema`."""

SCHEMA_ACTION = "schema"
"""The action name of a viewset's `/schema` endpoint."""


class EndpointDoc(TypedDict, total=False):
    """OpenAPI fields for one endpoint; a key not given keeps the mixin's own value."""

    summary: str
    description: str
    response_description: str
    deprecated: bool
    tags: list[str]
    parameters: dict[str, str]
    """Descriptions of the endpoint's path, query and header parameters, by their name in the schema."""
    responses: dict[int | str, dict[str, Any]]
    """`responses` entries by status, merged with `merge_responses()` with the endpoint's entries of the same status."""
    openapi_extra: dict[str, Any]
    """Merged into the operation: `parameters` lists concatenated, dicts merged per key, other values replaced."""


ENDPOINT_DOC_KEYS = frozenset(EndpointDoc.__annotations__)
"""The keys an `@endpoint_docs` entry accepts."""


def endpoint_docs(docs: dict[str, EndpointDoc]):
    """
    Attach per-action documentation to a viewset; the key `'*'` documents every action but `schema`. Must go
    below `@route_viewset`, which reads it while building routes; the other order raises `ValueError`, as does
    a key of an entry outside `EndpointDoc`.
    """
    for action, entry in docs.items():
        unknown = set(entry) - ENDPOINT_DOC_KEYS
        if unknown:
            raise ValueError(
                f"@endpoint_docs entry {action!r} has key(s) outside EndpointDoc: {', '.join(sorted(unknown))}; "
                f"the keys are {', '.join(sorted(ENDPOINT_DOC_KEYS))}"
            )

    def decorator(cls):
        if getattr(cls, "__viewset_metadata__", None) is not None:
            raise ValueError(
                f"@endpoint_docs must go below @route_viewset on {cls.__name__} - decorators apply "
                f"bottom-up, and route_viewset reads the documentation while it builds the routes"
            )
        # the class's own declarations only: docs_for reads the bases' along the MRO
        setattr(cls, DOCS_ATTRIBUTE, {**cls.__dict__.get(DOCS_ATTRIBUTE, {}), **docs})
        return cls

    return decorator


def merge_openapi_extra(base: dict[str, Any] | None, extra: dict[str, Any] | None) -> dict[str, Any]:
    """
    `extra` merged into a copy of `base`: the `parameters` lists concatenated, dicts merged per key (recursively),
    any other value of `extra` replacing the one in `base`.
    """
    merged = copy.deepcopy(base or {})
    for key, value in copy.deepcopy(extra or {}).items():
        if key == "parameters":
            merged[key] = [*merged.get(key, []), *value]
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merged_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _merged_dicts(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        merged[key] = (
            _merged_dicts(merged[key], value)
            if isinstance(value, dict) and isinstance(merged.get(key), dict)
            else value
        )
    return merged


def _declared_entries(cls: type, action: str):
    """The entries that document `action`, from the base class down, each class's `'*'` before its own entry."""
    names = (action,) if action == SCHEMA_ACTION else (EVERY_ACTION, action)
    for base in reversed(cls.__mro__):
        declared = base.__dict__.get(DOCS_ATTRIBUTE, {})
        for name in names:
            entry = declared.get(name)
            if entry:
                yield name, entry


def docs_for(cls: type, action: str) -> dict[str, Any]:
    """
    The documentation of one action, merged along the MRO, each class's `'*'` entry before its entry of the
    action. A later entry replaces a field; `parameters` merges per parameter, `responses` with
    `merge_responses()`, `openapi_extra` with `merge_openapi_extra()`.
    """
    merged: dict[str, Any] = {}
    for _name, entry in _declared_entries(cls, action):
        previous = dict(merged)
        merged.update(entry)
        if "parameters" in previous or "parameters" in entry:
            merged["parameters"] = {**previous.get("parameters", {}), **entry.get("parameters", {})}
        if "responses" in previous or "responses" in entry:
            merged["responses"] = merge_responses(previous.get("responses"), entry.get("responses"))
        if "openapi_extra" in previous or "openapi_extra" in entry:
            merged["openapi_extra"] = merge_openapi_extra(previous.get("openapi_extra"), entry.get("openapi_extra"))
    return merged


def action_parameter_names(cls: type, action: str) -> set[str]:
    """The parameter names the entries of `action` itself describe, without those of `'*'`."""
    return {
        name
        for entry_name, entry in _declared_entries(cls, action)
        if entry_name == action
        for name in entry.get("parameters", {})
    }


def operation_docs_for(
    cls: type, action: str, responses: dict | None = None, openapi_extra: dict | None = None
) -> dict[str, Any]:
    """
    `docs_for` without `parameters`, as keyword arguments of `add_api_route`: its `responses` merged over
    `responses` with `merge_responses()`, its `openapi_extra` merged into `openapi_extra` with
    `merge_openapi_extra()`; both keys are always present. A list the documentation declares in `openapi_extra`,
    other than the top-level `parameters`, replaces the generated operation's list (e.g. the `security` of
    `settings.viewsets_security_scheme`) instead of being appended to it.
    """
    docs = docs_for(cls, action)
    docs.pop("parameters", None)
    docs["responses"] = merge_responses(responses, docs.get("responses"))
    declared = docs.get("openapi_extra")
    docs["openapi_extra"] = merge_openapi_extra(openapi_extra, declared)
    _replace_declared_lists(docs["openapi_extra"], declared or {}, top_level=True)
    return docs


def _replace_declared_lists(merged: dict[str, Any], declared: dict[str, Any], top_level: bool = False) -> None:
    """
    Turn the lists of `merged` that `declared` sets into tuples: FastAPI appends a list of `openapi_extra` to the
    operation's list of the same key, but replaces it with any other value; its OpenAPI model returns a list.
    """
    for key, value in declared.items():
        if top_level and key == "parameters":
            continue
        if isinstance(value, list):
            merged[key] = tuple(merged[key])
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            _replace_declared_lists(merged[key], value)


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
