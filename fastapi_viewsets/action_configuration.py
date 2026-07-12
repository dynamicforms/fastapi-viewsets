from typing import Any

from .conf import settings
from .middleware import Middleware

_PERFORM_METHOD_BY_ACTION = {
    "create": "perform_create",
    "bulk_create": "perform_bulk_create",
    "list_items": "perform_list",
    "retrieve": "perform_retrieve",
    "update": "perform_update",
    "partial_update": "perform_update",
    "bulk_update": "perform_bulk_update",
    "bulk_partial_update": "perform_bulk_update",
    "destroy": "perform_destroy",
    "bulk_destroy": "perform_bulk_destroy",
    "lookup": "perform_lookup",
}
"""Mirrors the action-method -> perform_* wiring already in mixins.py, so @action_configuration on
a perform_* method (where CRUD logic actually lives) is found when resolving its action's config,
not just on the framework's own @final action methods (update, list_items, ...)."""


def action_configuration(config: dict[Any, Any]):
    """
    Attaches per-viewset/per-action configuration for context processors and command middleware to
    consult - works identically whether applied to a viewset class or to an individual method
    (typically a perform_* method). Keys are identifiers (a context processor callable, a
    Middleware subclass already registered in settings.viewsets_command_middleware, or a fresh
    Middleware instance to inject just for this call - see extra_middlewares_for); values are
    opaque, interpreted only by whichever processor/middleware reads them (see
    fastapi_viewsets/context/__init__.py's Context.configuration_for and build_context). See
    docs/guide/action-configuration.md.

        @action_configuration({Session: False})
        class LoginViewSet(...): ...

        class ItemViewSet(...):
            @action_configuration({Session: False})
            async def perform_list(self, context): ...
    """

    def decorator(target):
        target.__action_configuration__ = config
        return target

    return decorator


def _candidate_method_names(action_name: str) -> list[str]:
    names = [action_name]
    perform_name = _PERFORM_METHOD_BY_ACTION.get(action_name)
    if perform_name:
        names.append(perform_name)
    return names


def resolve_action_configuration(req_instance: Any, action_name: str) -> dict[Any, Any]:
    """
    Merges settings.default_action_configuration (lowest priority) -> class-level
    @action_configuration -> method-level @action_configuration (highest priority), per identifier
    (a method-level entry for an identifier fully replaces whatever a lower layer had for that same
    identifier; identifiers a method doesn't mention still fall through from the class/default).

    Returns the raw merged dict - ByAction values are left unresolved (see
    fastapi_viewsets/context/__init__.py's Context.configuration_for, which resolves them at read
    time against the current action, so a Context reused across actions - e.g. via
    clone_for_command - never resolves stale).

    Method-level lookup checks both the actual action method name (for hand-written __router
    endpoints, which have no separate perform_* counterpart) and its mapped perform_* method (where
    CRUD logic actually lives - see _PERFORM_METHOD_BY_ACTION), in that order, so a perform_*-level
    override wins if both happen to be decorated.
    """
    merged: dict[Any, Any] = dict(settings.default_action_configuration)
    merged.update(getattr(type(req_instance), "__action_configuration__", {}))
    for candidate in _candidate_method_names(action_name):
        method_config = getattr(getattr(req_instance, candidate, None), "__action_configuration__", None)
        if method_config:
            merged.update(method_config)
    return merged


def extra_middlewares_for(action_configuration: dict[Any, Any], global_middlewares: list) -> list:
    """
    Keys in `action_configuration` that are a Middleware *instance* are appended to the chain,
    just for this call, in dict-iteration order - this is how @action_configuration injects a
    brand-new middleware for one viewset/method without registering it globally. Keys that are a
    Middleware *class* configure an already-globally-registered instance of that type (read via
    Middleware.config_from/Context.configuration_for) - a class with no matching instance in
    `global_middlewares` raises ValueError, fail-fast, since it's almost certainly a typo or a
    forgotten global registration rather than an intentional injection (use an instance for that).
    """
    global_types = {type(mw) for mw in global_middlewares}
    extras = []
    for identifier in action_configuration:
        if isinstance(identifier, Middleware):
            extras.append(identifier)
        elif isinstance(identifier, type) and issubclass(identifier, Middleware) and identifier not in global_types:
            raise ValueError(
                f"{identifier.__name__} is configured via @action_configuration but has no instance "
                f"in settings.viewsets_command_middleware - register one globally, or pass a "
                f"Middleware instance (not the class) to inject a new one just for this call."
            )
    return extras
