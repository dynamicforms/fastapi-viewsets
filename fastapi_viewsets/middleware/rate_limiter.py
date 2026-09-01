import inspect
import time

from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING

from ..exceptions import RateLimitedError
from . import Middleware, ViewSetResult

if TYPE_CHECKING:
    from fastapi import Request
    from redis.asyncio import Redis

    from ..context import Context

KeyFunc = Callable[["Request | None", type, "Context"], "str | Awaitable[str]"]


class RateLimiter(Middleware):
    """
    Fixed-window request-count limiting, per identity key. Configure the requests-per-window limit
    via `@action_configuration` (see `fastapi_viewsets.action_configuration`), keyed by this class
    - a resolved value of `None` (unconfigured) falls back to `default_limit`:

        limiter = RateLimiter(default_limit=100, window_seconds=60)
        settings.viewsets_command_middleware = [limiter]

        @action_configuration({RateLimiter: 10})   # stricter limit just for this viewset
        class ExpensiveViewSet(...): ...

    The whole check lives in `depends()` - bridged onto FastAPI's own `Depends()` by route_viewset,
    so an exceeded limit is rejected before FastAPI even parses the request body. `__call__` (the
    onion command-middleware chain) is therefore a trivial passthrough.

    The identity key defaults to `"<ViewSetClassName>:<client IP>"` - override via `key_func` (also
    receives the built `Context`, so e.g. `await context.user` can rate-limit per authenticated
    user instead of per IP; `key_func` may itself be sync or async, same as the callable
    `Authorization` config):

        RateLimiter(default_limit=100, key_func=lambda request, cls, context: context.user)

    Storage defaults to an in-memory dict - correct for a single-process deployment (and tests),
    but each worker process would track its own counts independently. Pass a `redis_client`
    (`redis.asyncio.Redis`) for a shared, multi-process-correct counter (`INCR` + `EXPIRE` on first
    increment in a window - the same fixed-window algorithm either way).
    """

    def __init__(
        self,
        default_limit: int,
        window_seconds: float = 60.0,
        redis_client: "Redis | None" = None,
        key_func: "KeyFunc | None" = None,
    ):
        self.default_limit = default_limit
        self.window_seconds = window_seconds
        self.redis_client = redis_client
        self.key_func = key_func or self._default_key
        self._counts: dict[str, tuple[int, float]] = {}

    @staticmethod
    def _default_key(request: "Request | None", cls: type, _context: "Context") -> str:
        client_host = request.client.host if request is not None and request.client is not None else "unknown"
        return f"{cls.__name__}:{client_host}"

    async def depends(self, request: "Request | None", cls: type, context: "Context") -> None:
        config = self.config_from(context)
        limit = self.default_limit if config is None else config
        key = self.key_func(request, cls, context)
        if inspect.isawaitable(key):
            key = await key
        allowed = await self._increment(key, limit)
        if not allowed:
            raise RateLimitedError()

    async def __call__(
        self,
        _request: "Request | None",
        _viewset: Any,
        _context: "Context",
        call_next: Callable[[], Awaitable[ViewSetResult]],
    ) -> ViewSetResult:
        return await call_next()

    async def _increment(self, key: str, limit: int) -> bool:
        if self.redis_client is not None:
            count = await self.redis_client.incr(key)
            if count == 1:
                await self.redis_client.expire(key, int(self.window_seconds))
            return count <= limit
        return self._increment_in_memory(key, limit)

    def _increment_in_memory(self, key: str, limit: int) -> bool:
        now = time.monotonic()
        count, window_start = self._counts.get(key, (0, now))
        if now - window_start >= self.window_seconds:
            count, window_start = 0, now
        count += 1
        self._counts[key] = (count, window_start)
        return count <= limit
