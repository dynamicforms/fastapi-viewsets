# Idempotent Calls

`fastapi_viewsets.middleware.idempotency.Idempotency` is a [command middleware](./command-middleware)
that makes a call repeatable. The client sends an `Idempotency-Key` with the call and repeats the call
with the same key when the answer is lost (a timeout, a dropped socket); the server answers the repeat
with the outcome of the first call instead of running the action again.

The key and its rules follow
[The Idempotency-Key HTTP Header Field](https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-idempotency-key-header)
(`draft-ietf-httpapi-idempotency-key-header-07`), with one difference: a repeat of a running call
attaches to it and receives its outcome, where the draft answers `409`.

---

## Configuration

```python
from redis.asyncio import Redis

from fastapi_viewsets.conf import settings
from fastapi_viewsets.middleware.idempotency import Idempotency

settings.viewsets_command_middleware = [Idempotency(redis_client=Redis.from_url("redis://localhost")), ...]
settings.viewsets_idempotency_ttl = 24 * 3600   # seconds; the default
settings.viewsets_idempotency_lock_ttl = 600    # seconds; the default
```

`Idempotency` is placed first in `viewsets_command_middleware`: a middleware ahead of it runs again
on every repeat. Being a command middleware, it serves REST and [muxws](./muxws) alike.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `redis_client` | `None` | A `redis.asyncio.Redis`. Without it the records live in the `Idempotency` instance, which serves a single process |
| `key_prefix` | `"idempotency:"` | The prefix of the Redis keys and channels |

The TTLs are read from `settings` on every call.

## The key

The key is a Structured Field string (RFC 8941): a quoted string of printable ASCII, in which `"` and
`\` are escaped with `\`.

```http
Idempotency-Key: "8e03978e-40d5-43e8-bc93-6894a57f9324"
```

The client generates a new key (a UUID) for every call, `GET` included, and sends the same key only
when it repeats that call. A header that is not a single, non-empty Structured Field string is
rejected from `depends()`, before the request body is parsed.

## Rules

| Call | Record for the key | Answer |
|------|--------------------|--------|
| no `Idempotency-Key` | — | the action runs; nothing is recorded |
| key | none | the record is written as running, the action runs, the outcome is stored or the record deleted |
| key, same fingerprint | running | attaches; the outcome of the running call when it completes |
| key, same fingerprint | outcome | the stored outcome |
| key, different fingerprint | any | `422`, `detail_code` `idempotency_key_reused` |
| key that is not a Structured Field string | — | `400`, `detail_code` `idempotency_key_invalid` |

- **Fingerprint.** The method, path, query string and the SHA-256 of the body of the call. A body
  FastAPI read as a form is hashed as its sorted fields, a file field as its file name and size.
- **A record exists from the moment the action starts.** The record is written when the command
  middleware chain reaches `Idempotency`. A call rejected before that (authentication, a `depends()`
  rejection, a `422` from request validation, rate limiting) leaves no record, and its repeat is a new
  call.
- **A call runs to the end of its action** whether or not its client is still connected. Over HTTP
  Starlette does not interrupt an application when the client disconnects; over muxws a stream that
  ends early does not cancel the request (see [muxws](./muxws#a-stream-that-ends-early)). The outcome
  is recorded as for any other call.
- **Outcome.** The `ViewSetResult` of the chain below the middleware: body, headers and status code.
  A repeat returns a copy of it, and FastAPI serialises it through the same response model as the
  first answer. With `redis_client` the body is stored as `jsonable_encoder` encodes it (a model by
  its aliases), which the response model validates again on a repeat; an outcome that cannot be
  encoded is not stored.
- **Error outcomes are stored too.** An `HTTPException` is raised again on a repeat with its status
  code, `detail` and headers, a `DfViewSetError` with its `code` and `params`, and with its class when
  `fastapi_viewsets.exceptions` defines it (an `ActionInterruptedError` is replayed as one). A
  `RequestValidationError` raised by the action is replayed as a `422` with its errors as `detail`.
  Any other exception is replayed as a `500` with `detail` `"Internal Server Error"`.
- **A call that did not run is not an outcome.** For an `ActionDispatchError` (a `celery_viewset`
  task that could not be sent, or that the worker revoked, rejected or did not know) the record is
  deleted, and a repeat runs the call again.
- **What is stored depends on the method.**
  - `POST`, `PUT`, `PATCH`, `DELETE`: the outcome is stored.
  - `GET`, `HEAD`: the outcome is stored only when a repeat attached to the call while it ran. A
    repeat of a finished `GET` whose outcome was not stored runs again.
  - An outcome with a cookie or a `set-cookie` header is not stored, whatever the method: its record
    is deleted when the action completes, and a repeat runs the action again, an attached repeat
    included.
- **A stored outcome expires** `settings.viewsets_idempotency_ttl` seconds after it was stored.
- **A running record is extended while the call runs.** The process running the call extends it to
  `settings.viewsets_idempotency_lock_ttl` seconds every third of that time, so a call that runs
  longer than the lock TTL keeps its record. A process that dies mid-action stops extending it; once
  it expires, a repeat runs the action again, and so does a repeat attached to it at that moment. The
  outcome of the first call, when it finishes after that, does not replace the record of the call
  that took the key over.
- **A cancelled call** (its task is cancelled) leaves no record; a repeat attached to it runs the
  action again.

## Storage

Without `redis_client` the records are a dictionary of the `Idempotency` instance. A repeat attaches
to a running call by awaiting a future of the call.

With `redis_client`:

| Redis key | Content |
|-----------|---------|
| `<key_prefix><key>` | The record, a JSON string: the fingerprint and the owner of the running call, or the fingerprint and the outcome |
| `<key_prefix><key>:attached` | Set by a repeat on another process that attached to the running call, with the running record's remaining lifetime; read for a `GET` or `HEAD` |
| channel `<key_prefix><key>` | A message is published when the call completes |

- The running record is written with `SET NX PX` and the lock TTL in milliseconds. Every third of the
  lock TTL a Lua script extends it, and the `:attached` key when it exists, to the lock TTL, provided
  it is still the record this process wrote.
- On completion one Lua script replaces the running record with the outcome (with the TTL in
  milliseconds) or deletes it, provided it is still the record this process wrote. The process then
  publishes on the key's channel, also when the script failed, and resolves the futures of the
  repeats it holds, in that order.
- A repeat on the process running the call awaits that call's future. A repeat on another process
  reads the record, sets the `:attached` key through a Lua script that sets it only while that
  running record still exists, subscribes to the channel, reads the record again (an outcome stored
  before the subscription) and waits for the message or for the running record to expire.

## Frontend

The npm proxies send an `Idempotency-Key` with every call, over REST and over muxws, and repeat a
call with the same key when an attempt ends without an answer from the application.

### The key

Every call gets a new key, a version 4 UUID (`crypto.randomUUID()`, or the same value built from
`crypto.getRandomValues()` outside a secure context). The promise a proxy method returns is a
`ViewSetCall<R>`: a `Promise<R>` with a read-only `key`, readable before the call settles.

```ts
const call = items.create({ name: 'x' });
call.key;                  // '8e03978e-40d5-43e8-bc93-6894a57f9324'
const item = await call;
```

`key` is set on the promise of every public method of a proxy, the built-in actions and the
ViewSet's own methods alike. A method gets the key of the first request it issues before its first
`await`; `async count() { return this.request(...) }` has it, a method that awaits something before
its first request returns a promise without `key`. The declared actions of a ViewSet are typed
`ViewSetActionCall<R>`, in which `key` is optional, so that an override may return a plain
`Promise<R>`. `route_rest` and `route_muxws` type every method as returning `ViewSetCall<R>`.

`RequestOptions.key` sets the key of one call. An application that repeats a call itself passes the
`key` of the first one:

```ts
class ItemApi extends restViewSet<Item>()('id', [ViewSetMixin]) {
  async approve(pk: number, key?: string): Promise<Item> {
    return this.request<Item>('POST', `/${pk}/approve`, { key });
  }
}
```

A key that is empty or holds a character outside printable ASCII rejects the call with a
`TypeError` before anything is sent.

### Repeats

| Transport | An attempt that ended without an answer | Repeated |
|-----------|-----------------------------------------|----------|
| REST | an axios error without `response`, other than a cancellation | at once after the attempt's timeout (`ECONNABORTED`, `ETIMEDOUT`), after the delay otherwise |
| REST | `502`, `503` or `504` whose body is not a JSON object with `detail` | after the delay |
| muxws | the stream failed with `StreamTimeout` | at once |
| muxws | the stream failed with `ConnectionLost` or `StreamRefused`, or `open()` threw `ConnectionClosed` (the peer is between sockets) or `ConnectionGoingAway` (its connection drains after a goaway) while the peer has not closed for good | after the delay, and once the peer has reconnected |

Any other outcome settles the call. A `502`, `503` or `504` with a JSON body holding `detail` is the
body FastAPI gives an `HTTPException`, so the status is the application's answer (e.g.
`result_timeout` of a `celery_viewset` call); `Idempotency` stores it, and it settles the call as it
does over muxws, where a status is never repeated.

A repeated attempt is abandoned first: its axios request is aborted through its `AbortSignal`, its
muxws stream is cancelled. Only the last attempt settles the promise, and `key` stays the same across
attempts.

Every attempt is sent with the headers of the first: over REST the headers of the first attempt's
axios `config` after the request interceptors (other than `Accept`, `Accept-Encoding`,
`Content-Length`, `Content-Type` and `User-Agent`, which axios sets again), over muxws the `headers` of
the first stream, after the peer's open interceptors. A header an interceptor adds from state bound
for the caller's synchronous part (such as a taskwire `run()` token) thus reaches every repeat. An
attempt that failed before axios built its config, or before a stream was opened, has no headers to
carry over. `Idempotency-Key` is matched in any letter case: a header of that name in the proxy's or
the call's headers is replaced by the call's key.

```ts
const items = new ItemApi({
  basePath: '/items',
  attemptTimeoutMs: 30_000,
  repeat: { withinMs: 600_000, delayMs: 1000 },
});
```

| Option | Default | Effect |
|--------|---------|--------|
| `attemptTimeoutMs` | `30000` | REST only: the timeout of one attempt, passed to axios as `timeout`; `0` sets none. Over muxws, `timeoutMs` is the timeout of one attempt |
| `repeat.withinMs` | `600000` | A repeat is sent only this long after the first attempt; past it the call rejects with the error of its last attempt. `0` turns repeating off |
| `repeat.delayMs` | `1000` | The delay before a repeat after a lost connection or a gateway status, doubled after each, at most 30000 |

`repeat` is an option of every proxy and of `RequestOptions`, where it applies to one call. The
startup schema check is not repeated.

A repeat over muxws waits for `peer.onReconnect`, at most until `withinMs` has elapsed. A peer that
closes for good (`onClose` with `willRetry` false) ends the wait, and the call rejects with the
`ConnectionClosed` its next `open()` throws.

### Call activity

`onCallActivity(listener)` reports every call issued through a proxy, over REST and muxws: `start`
when it is issued, `retry` for each repeat, with how the attempt ended and the delay before the
repeat, and `settle` when its promise settles. Each event carries the call's `key`. A busy indicator
or a connectivity notice builds on it; `RequestOptions.background: true` marks a call such an
indicator leaves out, and the startup schema check is sent with it. `pendingCalls()` lists the calls
that have not settled, oldest first, with when each was issued and how many of its attempts went
unanswered; `pendingCallCount()` counts them, background calls left out. `retryNow()` sends every
waiting repeat at once, for a "try again now" button.

```ts
import { onCallActivity, retryNow } from '@dynamicforms/fastapi-viewsets';

const failures = new Map<string, number>();
const stop = onCallActivity((event) => {
  if (event.type === 'retry') failures.set(event.key, event.attempt);
  if (event.type === 'settle') failures.delete(event.key);
});
```

Without `Idempotency` on the backend, a repeat runs the action again. A deployment whose backend does
not use it sets `repeat: { withinMs: 0 }` on its proxies.

`Idempotency-Key` is not a CORS-safelisted header: a REST backend on another origin lists it in
`Access-Control-Allow-Headers` (`CORSMiddleware(allow_headers=[..., "Idempotency-Key"])`), and every
call to it, `GET` included, is preflighted.

## Known limitations

- Records are not scoped to a caller: a key is identified by its value alone. Two callers that send
  the same key and the same request share one record. A client-generated UUID makes this unlikely.
- In-memory storage (the default) serves one process; a repeat that reaches another process runs the
  action again.
- A repeat of a call that raised an exception other than `HTTPException` or `RequestValidationError`
  answers `{"detail": "Internal Server Error"}` through FastAPI's `HTTPException` handling, not through
  the application's handler for `Exception`.
- A `celery_viewset` call answered `504 result_timeout` or `503 result_reader_stopped` stores that
  outcome, although the worker may complete the action later; a repeat answers the stored error.
