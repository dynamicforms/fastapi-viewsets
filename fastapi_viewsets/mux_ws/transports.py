"""Per-endpoint control over which transports an action is published on."""

from collections.abc import Callable
from typing import TypeVar

F = TypeVar("F", bound=Callable)


def transports(*, rest: bool = True, muxws: bool | None = None) -> Callable[[F], F]:
    """
    Declares which transports a custom endpoint is published on.

    Applied *outside* the router decorator, because the router captures the function object at
    decoration time and this only marks it:

        class ItemViewSet(...):
            __router = APIRouter()

            @transports(rest=False)
            @__router.get("live", tags=["Item"])
            async def live(self, context: Context) -> list[Item]:
                ...

    `rest` is a plain bool: REST is the baseline, and an endpoint either appears there or does
    not. `muxws` is tri-state - None (the default) means "no opinion", deferring to
    `route_viewset(register_muxws=...)` and then to `settings.viewsets_register_muxws`, so that
    marking one endpoint does not silently opt its whole viewset in or out.

    An endpoint published on neither transport is unreachable, which is almost certainly a
    mistake, so it is refused here rather than quietly disappearing.
    """
    if rest is False and muxws is False:
        raise ValueError("transports(rest=False, muxws=False) would leave the endpoint unreachable")

    def decorator(func: F) -> F:
        func.__register_rest__ = rest
        func.__register_muxws__ = muxws
        return func

    return decorator
