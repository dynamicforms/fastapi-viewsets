from collections.abc import Callable
from typing import Any, Literal, TypeVar

T = TypeVar("T")

LifecycleType = Literal["singleton", "per-request", "instance-key"]


async def lifecycle_runner(
    original_endpoint: Callable,
    singleton_instance: T,
    cls: type[T],
    lifecycle: LifecycleType = "singleton",
    *args,
    response: Any = None,
    **kwargs
):
    """
    `response` is reserved for `route_viewset`'s optional `finalize_response` hook (see below) -
    callers that have no live Response object (e.g. `celery_viewset_server`, running in a worker
    process) simply omit it, and `finalize_response` is never invoked.
    """

    async def run_with_state(req_instance: Any):
        if hasattr(req_instance, "load_state"):
            await req_instance.load_state()

        try:
            bound_method = getattr(req_instance, original_endpoint.__name__, None)
            if bound_method is not None:
                return await bound_method(*args, **kwargs)
            return await original_endpoint(req_instance, *args, **kwargs)
        finally:
            if hasattr(req_instance, "save_state"):
                await req_instance.save_state()

    async def finalize(req_instance: Any, result: Any) -> Any:
        # Opt-in hook, mirroring load_state/save_state: only called when the viewset defines it
        # AND a live Response was actually supplied (route_viewset HTTP wiring, not celery_viewset
        # worker execution - a Celery worker has no Response object to hand it). Lets the viewset
        # translate part of its (JSON-serializable) return value into response-level side effects
        # (e.g. Set-Cookie) that can't survive a Celery/Redis round trip themselves - see
        # docs/guide/routers.md "Response-level side effects: finalize_response".
        if response is not None and hasattr(req_instance, "finalize_response"):
            return await req_instance.finalize_response(response, result)
        return result

    if lifecycle == "singleton":
        result = await run_with_state(singleton_instance)
        return await finalize(singleton_instance, result)
    elif lifecycle == "per-request":
        req_instance = cls()
        bound_method = getattr(req_instance, original_endpoint.__name__, None)
        if bound_method is not None:
            result = await bound_method(*args, **kwargs)
        else:
            result = await original_endpoint(req_instance, *args, **kwargs)
        return await finalize(req_instance, result)
    elif lifecycle == "instance-key":
        req_instance = cls()
        result = await run_with_state(req_instance)
        return await finalize(req_instance, result)

    return await original_endpoint(*args, **kwargs)
