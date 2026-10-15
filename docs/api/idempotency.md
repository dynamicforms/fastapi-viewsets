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

## Frontend

```ts
import {
  IDEMPOTENCY_KEY_HEADER,
  idempotencyKeyHeader,
  issueCall,
  newCallKey,
  type RepeatOptions,
  type ViewSetActionCall,
  type ViewSetCall,
  type ViewSetCalls,
} from '@dynamicforms/fastapi-viewsets';
```

The behaviour is described in [Idempotent Calls → Frontend](../guide/idempotency#frontend).

### Types

```ts
/** The promise a call returns, with the call's Idempotency-Key. */
type ViewSetCall<R> = Promise<R> & { readonly key: string };

/** What a declared action returns in a ViewSet's type; `key` is optional so an override may return a Promise. */
type ViewSetActionCall<R> = Promise<R> & { readonly key?: string };

/** `M` with every method that returns `Promise<R>` returning `ViewSetCall<R>`; other members unchanged. */
type ViewSetCalls<M> = {
  [P in keyof M]: M[P] extends (...args: infer A) => Promise<infer R> ? (...args: A) => ViewSetCall<R> : M[P];
};
```

| Where | Return type |
|-------|-------------|
| `request()` (protected) | `ViewSetCall<R>` |
| the actions of the mixin interfaces, `ViewSetProxyBase`, `RestProxyImpl`, `MuxwsProxyImpl` and factory-built ViewSets | `ViewSetActionCall<R>` |
| `route_rest<M>()`, `route_muxws<M>()` | `ViewSetCalls<M>` |
| a ViewSet's own method | as declared; `key` is set at run time when the method issues a request before its first `await` |

### RepeatOptions

```ts
interface RepeatOptions {
  withinMs?: number;
  delayMs?: number;
}
```

| Field | Default | Description |
|-------|---------|-------------|
| `withinMs` | `600000` | Milliseconds after the first attempt within which a repeat may be sent; past it the call rejects with the error of its last attempt. `0` turns repeating off |
| `delayMs` | `1000` | Milliseconds before a repeat after a lost connection or a gateway status, doubled after each such repeat, at most `30000`. A repeat after an attempt's own timeout is sent without a delay |

`repeat?: RepeatOptions` is an option of `route_rest`, `route_muxws`, `restViewSet` and
`muxwsViewSet` ViewSets and of `RequestOptions`, where it overrides the proxy's for one call.
`attemptTimeoutMs?: number` (default `30000`, `0` for none) is a REST option: the axios `timeout` of
one attempt.

`RequestOptions.key` and `RequestOptions.repeat` are listed with
[`RequestOptions`](./vue-mixins#requests-and-actions).

### Functions

| Name | Description |
|------|-------------|
| `IDEMPOTENCY_KEY_HEADER` | `'Idempotency-Key'` |
| `newCallKey(): string` | A version 4 UUID: `crypto.randomUUID()`, or built from `crypto.getRandomValues()` where `randomUUID` is absent (an insecure context) |
| `idempotencyKeyHeader(key: string): string` | `key` as a Structured Field string: quoted, `"` and `\` escaped. Throws `TypeError` for an empty key or one with a character outside printable ASCII |
| `issueCall<R>(promise: Promise<R>, key: string): ViewSetCall<R>` | Sets `key` on `promise` and returns it. Called synchronously from a transport's `request()`, it also gives `key` to the promise of the proxy method running around it |
