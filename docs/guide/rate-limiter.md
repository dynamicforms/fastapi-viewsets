# Rate Limiter

`fastapi_viewsets.middleware.rate_limiter.RateLimiter` is a ready-to-use, fixed-window
request-count limiting command middleware - one of a few
[built-in middleware/processor implementations](./authentication) this library ships, built on the
general [Command Middleware](./command-middleware) and [Action Configuration](./action-configuration)
mechanisms.

::: tip Where this fits
See [Architecture](./architecture#requirement-per-viewset-configuration) for why per-viewset
configuration (the `default_limit` override below) needed a general mechanism rather than another
one-off class attribute.
:::

---

## Configuring the limit

Keyed by identity (default: `"<ViewSetClassName>:<client IP>"`):

```python
from fastapi_viewsets.conf import settings
from fastapi_viewsets.middleware.rate_limiter import RateLimiter

settings.viewsets_command_middleware = [RateLimiter(default_limit=100, window_seconds=60)]
```

The limit is configurable per viewset/action via
[`@action_configuration`](./action-configuration), keyed by the `RateLimiter` class - a resolved
value of `None` (unconfigured) falls back to `default_limit`:

```python
@action_configuration({RateLimiter: 10})   # a stricter limit just for this viewset
class ExpensiveViewSet(...): ...
```

Exceeding the limit returns `429` with `detail` "Rate limit exceeded" and `detail_code`
`fastapi-viewsets.rate_limited` (see [Error Codes](./error-codes)) - raised from `depends()`
(bridged onto FastAPI's own native `Depends()` by `route_viewset`, see
[Command Middleware](./command-middleware#early-rejection-middleware-depends)), so a request over
the limit is rejected before FastAPI parses the body, without calling `perform_*`.

## Identity key

The default key is `"<ViewSetClassName>:<client IP>"` - override via `key_func(request, cls,
context)` (`cls` is the viewset *class*, not an instance - see
[Command Middleware](./command-middleware#early-rejection-middleware-depends) for why). `key_func`
may itself be sync or async (checked the same way as `Authorization`'s callable config):

```python
def per_ip_key(request, cls, context):
    return f"{cls.__name__}:{request.client.host}"

RateLimiter(default_limit=100, key_func=per_ip_key)
```

Over [muxws](./muxws) the client IP is the WebSocket connection's address, given that
`process_command` is called with `connection=`; without it every command has the client
`("muxws", 0)`, and all muxws callers of a viewset share one count.

The built `Context` is available too, so a key function can rate-limit per authenticated user
instead of per IP, when [Authentication](./authentication) is configured. `RateLimiter` does not
require it; any `context` field, or none, works:

```python
async def per_user_key(request, cls, context):
    user = await context.user
    return f"{cls.__name__}:{user['id'] if user else 'anonymous'}"

RateLimiter(default_limit=100, key_func=per_user_key)
```

## Storage

Defaults to an in-memory dict - correct for a single process (and tests), but each worker process
in a multi-process deployment would track its own counts independently. Entries whose window has
passed are removed at most once per window.

Pass a `redis_client` (`redis.asyncio.Redis`) for a shared, multi-process-correct counter. Each
request runs one Lua script on the key `<key_prefix><identity key>` (`key_prefix` defaults to
`"ratelimit:"`): `INCR`, then `PEXPIRE` to `window_seconds` in milliseconds when the key has no
expiry. Both steps are atomic, so a key always expires. The algorithm is the same fixed window as
in memory:

```python
from redis.asyncio import Redis

RateLimiter(default_limit=100, redis_client=Redis.from_url("redis://localhost"))
```

## Known limitations

- In-memory storage (the default) only tracks counts within one process - see
  [Storage](#storage) above.
- Fixed-window counting is simple and cheap, but allows up to `2x` the limit right at a window
  boundary (e.g. a burst just before the window resets, followed by another burst right after) -
  not a sliding-window/token-bucket algorithm.
