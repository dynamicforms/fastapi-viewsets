# Rate Limiter

`fastapi_viewsets.middleware.rate_limiter.RateLimiter` is a ready-to-use, fixed-window
request-count limiting command middleware - one of a few
[built-in middleware/processor implementations](./authentication) this library ships, built on the
general [Command Middleware](./command-middleware) and [Action Configuration](./action-configuration)
mechanisms.

::: tip Where this fits
See [Architecture](./architecture#problem-one-boolean-per-concern-doesn-t-scale) for why per-viewset
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

Exceeding the limit returns `429` (`{"detail": "Rate limit exceeded"}`) without ever calling
`perform_*`.

## Identity key

The default key is `"<ViewSetClassName>:<client IP>"` - override via
`key_func(request, viewset, context)`:

```python
RateLimiter(default_limit=100, key_func=lambda request, viewset, context: str(id(context)))
```

The built `Context` is available too, so a key function can rate-limit per authenticated user
(`await context.user`) instead of per IP - handy if [Authentication](./authentication) is also
wired in, though `RateLimiter` doesn't require it (any `context` field, or none at all, works).

## Storage

Defaults to an in-memory dict - correct for a single process (and tests), but each worker process
in a multi-process deployment would track its own counts independently. Pass a `redis_client`
(`redis.asyncio.Redis`) for a shared, multi-process-correct counter (`INCR` + `EXPIRE` on the first
increment in a window - the same fixed-window algorithm either way):

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
