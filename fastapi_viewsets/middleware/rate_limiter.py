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

# INCR and the window's expiry in one atomic step. The expiry is set whenever the key has none
# (PTTL < 0), so a key always expires, including one that was written without an expiry.
_INCREMENT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if redis.call('PTTL', KEYS[1]) < 0 then
    redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
return count
"""


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
    so an exceeded limit is rejected before FastAPI parses the request body. `__call__` (the
    onion command-middleware chain) only calls `call_next()`.

    The identity key defaults to `"<ViewSetClassName>:<client IP>"` - override via `key_func` (also
    receives the built `Context`, so e.g. `await context.user` can rate-limit per authenticated
    user instead of per IP; `key_func` may itself be sync or async, same as the callable
    `Authorization` config):

        RateLimiter(default_limit=100, key_func=lambda request, cls, context: context.user)

    Storage defaults to an in-memory dict - correct for a single-process deployment (and tests),
    but each worker process would track its own counts independently. Entries whose window has
    passed are removed at most once per window. Pass a `redis_client` (`redis.asyncio.Redis`) for a
    shared, multi-process-correct counter: one Lua script increments `<key_prefix><key>` and sets
    its expiry to the window (in milliseconds) when the key has none - the same fixed-window
    algorithm either way.
    """

    def __init__(
        self,
        default_limit: int,
        window_seconds: float = 60.0,
        redis_client: "Redis | None" = None,
        key_func: "KeyFunc | None" = None,
        key_prefix: str = "ratelimit:",
    ):
        self.default_limit = default_limit
        self.window_seconds = window_seconds
        self.redis_client = redis_client
        self.key_func = key_func or self._default_key
        self.key_prefix = key_prefix
        self._counts: dict[str, tuple[int, float]] = {}
        self._next_prune = 0.0

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
            window_ms = max(1, int(self.window_seconds * 1000))
            count = await self.redis_client.eval(_INCREMENT_SCRIPT, 1, self.key_prefix + key, window_ms)
            return int(count) <= limit
        return self._increment_in_memory(key, limit)

    def _increment_in_memory(self, key: str, limit: int) -> bool:
        now = time.monotonic()
        if now >= self._next_prune:
            self._counts = {k: entry for k, entry in self._counts.items() if now - entry[1] < self.window_seconds}
            self._next_prune = now + self.window_seconds
        count, window_start = self._counts.get(key, (0, now))
        if now - window_start >= self.window_seconds:
            count, window_start = 0, now
        count += 1
        self._counts[key] = (count, window_start)
        return count <= limit
