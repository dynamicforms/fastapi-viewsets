from dataclasses import dataclass
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
"""The perform_* method of each action (as wired in mixins.py), whose @action_configuration also applies."""


def action_configuration(config: dict[Any, Any]):
    """
    Attaches configuration for context processors and command middleware to a viewset class or a
    method (typically perform_*). Keys are a context processor, a globally registered Middleware
    class, or a Middleware instance to inject for this viewset/action; values are interpreted by
    whoever reads them. See docs/guide/action-configuration.md.

        @action_configuration({Session: False})
        class LoginViewSet(...): ...
    """

    def decorator(target):
        target.__action_configuration__ = config
        return target

    return decorator


@dataclass(frozen=True)
class TimeLimits:
    """
    The time limits, in seconds, of a deferred action's execution, configured under the `TimeLimits`
    key and enforced by the executor (an in-process action has none). `soft` raises
    `ActionInterruptedError` inside the execution; `hard` kills its process. None leaves a limit to the
    executor's own configuration. See docs/guide/action-configuration.md.
    """

    soft: float | None = None
    hard: float | None = None


def _candidate_method_names(action_name: str) -> list[str]:
    names = [action_name]
    perform_name = _PERFORM_METHOD_BY_ACTION.get(action_name)
    if perform_name:
        names.append(perform_name)
    return names


def resolve_action_configuration(cls: type, action_name: str) -> dict[Any, Any]:
    """
    Merges settings.default_action_configuration, then the class's, then the method's
    @action_configuration, each replacing the previous layer's entry per key. The method layer is the
    action method, then its perform_* method. Takes the class, so it works before an instance exists.
    `ByAction` values are left for `Context.configuration_for` to resolve at read time.
    """
    merged: dict[Any, Any] = dict(settings.default_action_configuration)
    merged.update(getattr(cls, "__action_configuration__", {}))
    for candidate in _candidate_method_names(action_name):
        method_config = getattr(getattr(cls, candidate, None), "__action_configuration__", None)
        if method_config:
            merged.update(method_config)
    return merged


def extra_middlewares_for(action_configuration: dict[Any, Any], global_middlewares: list) -> list:
    """
    The Middleware instances among the keys of `action_configuration`, injected for this call in key
    order. A Middleware class key without an instance in `global_middlewares` raises ValueError.
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
