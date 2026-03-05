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
    **kwargs
):

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

    if lifecycle == "singleton":
        return await run_with_state(singleton_instance)
    elif lifecycle == "per-request":
        req_instance = cls()
        bound_method = getattr(req_instance, original_endpoint.__name__, None)
        if bound_method is not None:
            return await bound_method(*args, **kwargs)
        return await original_endpoint(req_instance, *args, **kwargs)
    elif lifecycle == "instance-key":
        return await run_with_state(cls())

    return await original_endpoint(*args, **kwargs)
