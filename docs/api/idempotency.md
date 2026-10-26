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
| `async __call__(request, viewset, context, call_next) -> ViewSetResult` | Runs `call_next()` under the key, attaches to the running call or returns the stored outcome; raises `IdempotencyKeyReusedError` for a key recorded with a different fingerprint, and raises a stored error outcome again. An events [gate run](../guide/events#the-gate-run) passes through |

A result whose `body` is a starlette `Response` with a buffered `body` (`Response`, `JSONResponse`,
`HTMLResponse`, `PlainTextResponse`, ...) is recorded as its status, raw headers without
`content-length` and body; with `redis_client` it is stored as
`{"response": {"status_code", "headers": [[name, value], ...], "body": <base64>}, "headers", "status_code"}`
under `result`. A repeat, an attached one included, receives a new `Response` with that status, those
headers, one `content-length` and the body, and no background task. A `StreamingResponse`, a
`FileResponse` and any other `Response` without a buffered body is not stored; a repeat runs the
action again. A recorded `set-cookie` header is a cookie: the outcome is not stored.

`modifies_response_shape` is `False`.

## Settings

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `settings.viewsets_idempotency_ttl` | `float` | `86400` | Seconds a stored outcome is kept |
| `settings.viewsets_idempotency_lock_ttl` | `float` | `600` | Seconds a running record is kept after it was last extended; the process running the call extends it every third of this time |

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
| `IdempotencyKeyInvalidError()` | 400 | `fastapi-viewsets.idempotency_key_invalid` | — |
| `IdempotencyKeyReusedError(key)` | 422 | `fastapi-viewsets.idempotency_key_reused` | `{key}` |

Both are `DfViewSetError`s; see [Error Codes](../guide/error-codes).

## Frontend

```ts
import {
  type CallEvent,
  IDEMPOTENCY_KEY_HEADER,
  idempotencyKeyHeader,
  inBackground,
  issueCall,
  newCallKey,
  onCallActivity,
  type PendingCall,
  pendingCallCount,
  pendingCalls,
  type RepeatOptions,
  retryNow,
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

/**
 * `M` with every method that returns `Promise<R>` returning `ViewSetCall<R>`; other members unchanged.
 * The list actions of a library list mixin keep their overloads, rebuilt from its row types (`ListRows`).
 */
type ViewSetCalls<M> = {
  [P in keyof M]: P extends ListAction
    ? [ListRowsOf<M>] extends [never]
      ? CallOf<M[P]>
      : ListCalls<ListRowsOf<M>['row'], ListRowsOf<M>['types']>[P] // ListMixin's overloads, returning ViewSetCall
    : CallOf<M[P]>;
};

/** `F` returning `ViewSetCall<R>` where it returns `Promise<R>`; `F` itself otherwise. */
type CallOf<F> = F extends (...args: infer A) => Promise<infer R> ? (...args: A) => ViewSetCall<R> : F;
```

| Where | Return type |
|-------|-------------|
| `request()` (protected) | `ViewSetCall<R>` |
| the actions of the mixin interfaces, `ViewSetProxyBase`, `RestProxyImpl`, `MuxwsProxyImpl` and factory-built ViewSets | `ViewSetActionCall<R>` |
| `route_rest<M>()` with an options object, `route_muxws<M>()` | `RestProxy<M>`, `MuxwsProxy<M>`: `ViewSetCalls<M>` and `watch()` |
| `route_rest<M>()` with positional arguments | `ViewSetCalls<M>` |
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

### CallEvent

```ts
type CallEvent =
  | { readonly type: 'start'; readonly key: string; readonly background: boolean }
  | { readonly type: 'retry'; readonly key: string; readonly failure: 'timeout' | 'lost'; readonly attempt: number; readonly delayMs: number }
  | { readonly type: 'settle'; readonly key: string; readonly ok: boolean; readonly error?: unknown };
```

| Event | When | Fields |
|-------|------|--------|
| `start` | The call is issued, synchronously in `request()` | `background`: `RequestOptions.background`, or `true` inside `inBackground` |
| `retry` | An attempt ended without an answer and a repeat will be sent | `failure`: how it ended; `attempt`: the number of the attempt that ended, 1 for the first; `delayMs`: the wait before the repeat |
| `settle` | The call's promise settled | `ok`: false when it rejected; `error`: the rejection reason, the value the call's promise rejects with (an `AxiosError`, a `ViewSetRequestError`, a muxws error), present when `ok` is false |

### PendingCall

```ts
interface PendingCall {
  readonly key: string;
  readonly background: boolean;
  readonly startedAt: number; // Date.now() when the call was issued
  readonly unanswered: number; // attempts that ended without an answer and were repeated
}
```

`RequestOptions.key`, `RequestOptions.repeat` and `RequestOptions.background` are listed with
[`RequestOptions`](./vue-mixins#requests-and-actions).

### Functions

| Name | Description |
|------|-------------|
| `IDEMPOTENCY_KEY_HEADER` | `'Idempotency-Key'` |
| `newCallKey(): string` | A version 4 UUID: `crypto.randomUUID()`, or built from `crypto.getRandomValues()` where `randomUUID` is absent (an insecure context) |
| `idempotencyKeyHeader(key: string): string` | `key` as a Structured Field string: quoted, `"` and `\` escaped. Throws `TypeError` for an empty key or one with a character outside printable ASCII |
| `issueCall<R>(promise: Promise<R>, key: string, background = false): ViewSetCall<R>` | Sets `key` on `promise` and returns it. Called synchronously from a transport's `request()`, it also gives `key` to the promise of the proxy method running around it. Reports `start` (with `background`, `true` also inside `inBackground`) to the `onCallActivity` listeners at once and `settle` (with the rejection reason as `error`) when `promise` settles |
| `inBackground<R>(fn: () => R): R` | Runs `fn` and returns what it returns. Every call a proxy issues while `fn` runs synchronously reports `background: true`; a call issued after an `await` inside `fn` does not. Calls nest |
| `onCallActivity(listener: (event: CallEvent) => void): () => void` | Calls `listener` for every `CallEvent` of every call issued through a proxy, REST and muxws alike; returns the function that removes it. A listener that throws is logged with `console.error` and affects neither the call nor the other listeners |
| `pendingCalls(): readonly PendingCall[]` | Every call issued through a proxy that has not settled, oldest first; a snapshot |
| `pendingCallCount(includeBackground = false): number` | The number of unsettled calls issued through a proxy; background calls are counted only with `includeBackground` |
| `retryNow(): void` | Ends the delay of every call waiting to send a repeat, so each sends it now; the delay of such a call's next repeat starts again from `repeat.delayMs`. A muxws call waiting for its peer to reconnect keeps waiting |
