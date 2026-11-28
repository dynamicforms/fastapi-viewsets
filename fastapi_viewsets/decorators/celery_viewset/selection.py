import re

from collections.abc import Iterable

from fastapi.routing import APIRoute

from ...action_configuration import _candidate_method_names, TimeLimits

ActionSelector = Iterable[str | re.Pattern[str]] | None
"""Action names (exact) and compiled patterns (`fullmatch`) that select a viewset's actions."""


def normalize_selection(
    include: ActionSelector, exclude: ActionSelector
) -> tuple[tuple[str | re.Pattern[str], ...] | None, tuple[str | re.Pattern[str], ...] | None]:
    """`include` and `exclude` as tuples. Raises ValueError when both are given or an entry is neither a
    string nor a compiled pattern."""
    if include is not None and exclude is not None:
        raise ValueError("celery_viewset takes include or exclude, not both")
    normalized = []
    for entries in (include, exclude):
        if entries is None:
            normalized.append(None)
            continue
        if isinstance(entries, (str, re.Pattern)):
            raise ValueError(f"include / exclude must be an iterable of action names or patterns, not {entries!r}")
        entries = tuple(entries)
        for entry in entries:
            if not isinstance(entry, (str, re.Pattern)):
                raise ValueError(f"include / exclude entry {entry!r} is neither an action name nor a re.Pattern")
        normalized.append(entries)
    return normalized[0], normalized[1]


def _action_routes(cls: type) -> dict[str, APIRoute]:
    """The class's routes by action name (`route.name`, else the endpoint's name), first route per name."""
    routes: dict[str, APIRoute] = {}
    for route in cls.__router.routes:
        routes.setdefault(route.name or route.endpoint.__name__, route)
    return routes


def _matches(entry: str | re.Pattern[str], action: str) -> bool:
    return entry == action if isinstance(entry, str) else entry.fullmatch(action) is not None


def _selected(cls: type, entries: tuple, actions: list[str]) -> set[str]:
    """The actions any entry selects. Raises ValueError for an entry that selects none."""
    selected: set[str] = set()
    for entry in entries:
        matched = {action for action in actions if _matches(entry, action)}
        if not matched:
            shown = entry.pattern if isinstance(entry, re.Pattern) else entry
            raise ValueError(
                f"celery_viewset entry {shown!r} matches no action of {cls.__name__}; "
                f"its actions are: {', '.join(sorted(actions))}"
            )
        selected |= matched
    return selected


def _check_no_time_limits(cls: type, route: APIRoute) -> None:
    """Raises ValueError when the action method or its perform_* method configures `TimeLimits`."""
    method_name = route.endpoint.__name__
    for candidate in _candidate_method_names(method_name):
        config = getattr(getattr(cls, candidate, None), "__action_configuration__", None) or {}
        if TimeLimits in config:
            raise ValueError(
                f"{cls.__name__}.{candidate} configures TimeLimits, but action {method_name!r} runs in "
                f"process (it is not selected for celery_viewset): an in-process action has no time limits"
            )


def deferred_routes(cls: type, include: tuple | None, exclude: tuple | None) -> dict[str, APIRoute]:
    """
    The routes of the actions that run in the executor, by action name: those `include` selects, all but
    those `exclude` selects, or every action when neither is given. The class's `__router` must be built.
    Raises ValueError for an entry that matches no action and for `TimeLimits` on a method of an action
    that runs in process.
    """
    routes = _action_routes(cls)
    actions = list(routes)
    if include is not None:
        deferred = _selected(cls, include, actions)
    elif exclude is not None:
        deferred = set(actions) - _selected(cls, exclude, actions)
    else:
        deferred = set(actions)
    for action, route in routes.items():
        if action not in deferred:
            _check_no_time_limits(cls, route)
    return {action: route for action, route in routes.items() if action in deferred}
