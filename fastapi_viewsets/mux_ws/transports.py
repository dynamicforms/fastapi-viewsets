"""Per-endpoint control over which transports an action is published on."""

from collections.abc import Callable
from typing import TypeVar

F = TypeVar("F", bound=Callable)


def transports(*, rest: bool = True, muxws: bool | None = None) -> Callable[[F], F]:
    """
    Declares which transports a custom endpoint is published on. Applied outside the router
    decorator (`@transports(rest=False)` above `@__router.get(...)`). `muxws=None` defers to
    `route_viewset(register_muxws=...)`, then `settings.viewsets_register_muxws`. Raises ValueError
    when both are False.
    """
    if rest is False and muxws is False:
        raise ValueError("transports(rest=False, muxws=False) would leave the endpoint unreachable")

    def decorator(func: F) -> F:
        func.__register_rest__ = rest
        func.__register_muxws__ = muxws
        return func

    return decorator
