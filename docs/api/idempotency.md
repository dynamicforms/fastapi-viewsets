# Idempotency — API Reference

```python
from fastapi_viewsets.middleware.idempotency import (
    IDEMPOTENCY_KEY_HEADER,
    Idempotency,
    parse_idempotency_key,
    read_idempotency_key,
    request_fingerprint,
)
from fastapi_viewsets.exceptions import IdempotencyKeyInvalidError, IdempotencyKeyReusedError
```

The rules are described in [Idempotent Calls](../guide/idempotency).

## Idempotency

```python
class Idempotency(Middleware):
    def __init__(self, redis_client: Redis | None = None, key_prefix: str = "idempotency:"): ...
```

A [`Middleware`](../guide/command-middleware#class-based-middleware-middleware).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `redis_client` | `redis.asyncio.Redis \| None` | `None` | Where the records live; `None` keeps them in the instance |
| `key_prefix` | `str` | `"idempotency:"` | Prefix of the record key, the `:attached` key and the channel |

| Method | Description |
|--------|-------------|
| `async depends(request, cls, context) -> None` | Raises `IdempotencyKeyInvalidError` for an invalid `Idempotency-Key` |
| `async __call__(request, viewset, context, call_next) -> ViewSetResult` | Runs `call_next()` under the key, attaches to the running call or returns the stored outcome; raises `IdempotencyKeyReusedError` for a key recorded with a different fingerprint, and raises a stored error outcome again |

`modifies_response_shape` is `False`.

## Settings

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `settings.viewsets_idempotency_ttl` | `float` | `86400` | Seconds a stored outcome is kept |
| `settings.viewsets_idempotency_lock_ttl` | `float` | `600` | Seconds a running record is kept unless the action finishes first |

## Functions

| Name | Description |
|------|-------------|
| `IDEMPOTENCY_KEY_HEADER` | `"Idempotency-Key"` |
| `parse_idempotency_key(value: str) -> str` | The key carried by a header value that is a Structured Field string; surrounding spaces and tabs are ignored. Raises `IdempotencyKeyInvalidError` for a token, a parameter after the string, an escape other than `\"` and `\\`, a character outside printable ASCII, or an empty string |
| `read_idempotency_key(request: Request) -> str \| None` | The key of the request's `Idempotency-Key` header, `None` without one. Raises `IdempotencyKeyInvalidError` for more than one such header or an invalid value |
| `async request_fingerprint(request: Request) -> str` | Hex SHA-256 over the method, path, query string and the hex SHA-256 of the body, joined with `\n`; a body read as a form is hashed as its sorted fields, a file field as `<filename>:<size>` |

## Errors

| Class | Status | `code` | `params` |
|-------|--------|--------|----------|
| `IdempotencyKeyInvalidError()` | 400 | `idempotency_key_invalid` | — |
| `IdempotencyKeyReusedError(key)` | 422 | `idempotency_key_reused` | `{key}` |

Both are `DfViewSetError`s; see [Error Codes](../guide/error-codes).
