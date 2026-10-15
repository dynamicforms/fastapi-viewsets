# Idempotent calls

Status: concept. Nothing described here is implemented yet.

## Purpose

A call whose response is lost (a timeout, a dropped socket, a phone losing signal) leaves the
client not knowing whether the action ran. Repeating a `POST` can create a second record; not
repeating it can lose the first one. This API makes every call repeatable: the client sends a key
with each call and repeats the call with the same key, and the server answers the repeat with the
outcome of the first call instead of running the action again.

A repeat that arrives while the first call is still running attaches to it and receives its
outcome when the action completes. A long action therefore needs no separate "collect the result"
endpoint: the client repeats the call whenever its own timeout expires, and the promise on the
frontend resolves to the same body a direct answer would have carried.

The key and its rules follow
[The Idempotency-Key HTTP Header Field](https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-idempotency-key-header)
(`draft-ietf-httpapi-idempotency-key-header-07`, an Internet-Draft), with one difference: the draft
answers a repeat of a running call with `409`, this API attaches the repeat to the running call
(rule 5).

Progress reporting is not part of this API. taskwire uses the key as the token of an operation; see
"taskwire".

## Vocabulary

| Term | Meaning |
|---|---|
| **key** | The value of `Idempotency-Key`, a UUID the frontend generates for one call. Sent as a Structured Field string: `Idempotency-Key: "8e03978e-40d5-43e8-bc93-6894a57f9324"`. |
| **repeat** | A call with a key the server has already seen. |
| **fingerprint** | Method, path, query string and a hash of the body of the call that first used a key. |
| **outcome** | The answer of the action: status, headers and body. |
| **record** | What the server keeps per key: the fingerprint, and either "running" or the outcome. |

## Rules

1. **The key identifies one call.** The frontend generates a new key for every call, `GET`
   included, and sends the same key only when it repeats that call.
2. **A record exists from the moment the action starts.** A call rejected before the action
   (authentication, a `depends()` rejection, a `422` from request validation, rate limiting) leaves
   no record, and its repeat is a new call.
3. **A call runs to the end of its action whether or not its client is still there.** Over HTTP,
   Starlette does not interrupt an application when the client disconnects. Over muxws, a stream
   that ends early does not cancel the request either (see "Changes elsewhere"). The answer to a
   client that has gone is discarded; the outcome is recorded as for any other call.
4. **A repeat of a finished call answers with the stored outcome.** Status, headers and body are
   those of the first answer. Error outcomes are stored too: an action that ran and failed has run.
5. **A repeat of a running call attaches to it** and answers with its outcome when the action
   completes. Any number of repeats can attach to one call.
6. **What is stored depends on the method.**
   - `POST`, `PUT`, `PATCH`, `DELETE`: the outcome is stored.
   - `GET`, `HEAD`: the outcome is stored only when a repeat attached to the call while it ran. A
     repeat of a finished `GET` whose outcome was not stored runs again.
   - An outcome with a `set-cookie` header is not stored, whatever the method: its record is deleted
     when the action completes, and a repeat runs the action again.
7. **A key reused with a different fingerprint answers `422`** (draft).
8. **A record expires `settings.viewsets_idempotency_ttl` after the outcome is stored.** A repeat
   after that is a new call.
9. **A running record expires `settings.viewsets_idempotency_lock_ttl` after it was written**
   unless the action finishes first. A process that dies mid-action leaves a running record; once
   it expires, a repeat runs the action again, and so does a repeat attached to it at that moment.

## Backend

### The middleware

```python
from fastapi_viewsets.middleware.idempotency import Idempotency

settings.viewsets_command_middleware = [Idempotency(redis_client=redis), ...]
settings.viewsets_idempotency_ttl = 24 * 3600        # seconds; the default
settings.viewsets_idempotency_lock_ttl = 600         # seconds; the default
```

`Idempotency` is a command middleware, so it runs on REST and muxws alike. It is placed first in
`viewsets_command_middleware`: a middleware ahead of it would run again on every repeat. Without
`redis_client` the records live in the process, which suits a single process; with one they live in
Redis, which a repeat landing on another process needs.

| Call | Record for the key | Answer |
|---|---|---|
| no `Idempotency-Key` | — | the action runs; nothing is recorded |
| key | none | the record is written as running (atomic `SET NX`), the action runs, the outcome is stored or the record deleted (rule 6) |
| key, same fingerprint | running | attaches; the outcome of the running call |
| key, same fingerprint | outcome | the stored outcome |
| key, different fingerprint | any | `422`, `detail_code` `idempotency_key_reused` |
| key that is not a Structured Field string | — | `400`, `detail_code` `idempotency_key_invalid` |

The errors are `DfViewSetError`s, with `detail_code` and `detail_params` like every other error of
the library (see [Error codes](docs/guide/error-codes.md)).

The stored outcome is the `ViewSetResult` of the chain below the middleware: body, status code and
headers. A repeat returns it as its own `ViewSetResult`, so FastAPI serialises it through the same
response model as the first answer.

### Attaching

In one process, the middleware keeps a future per running key, and a repeat awaits it.

With Redis, a repeat on another process:

1. marks the record as attached, so that a `GET` outcome is stored (rule 6);
2. subscribes to the key's channel;
3. reads the record again, which covers an outcome stored before the subscription;
4. waits for the message the running process publishes after storing the outcome, or for the running
   record to expire (rule 9).

The running process stores the outcome, publishes on the key's channel and resolves its own futures,
in that order.

## Frontend

### The key

The proxy generates a key for every call (`crypto.randomUUID()`), sends it as `Idempotency-Key`,
and exposes it on the promise the call returns:

```ts
const call = items.create({ name: 'x' });
call.key;                  // '8e03978e-40d5-43e8-bc93-6894a57f9324'
const item = await call;
```

Every public method of a proxy returns a `ViewSetCall<R>`: a `Promise<R>` with a read-only `key`.
The proxy wraps each method; the wrapper reads the key of the first request the method issues and
attaches it to the promise the method returns. The method has to issue that request before its
first `await`. Every built-in method does, and so does a custom method of the form
`async count() { return this.request(...) }`. A method that awaits something first returns a
promise with no `key`.

`RequestOptions.key` sets the key of one call. An application that repeats a call itself passes the
`key` of the first one.

### Repeats

The proxy repeats a call, with the same key, when an attempt ends without an answer from the
application:

| Transport | No answer |
|---|---|
| REST | an axios error without `response` (network error, the attempt's timeout); `502`, `503` or `504` |
| muxws | the stream ended with `ConnectionLost`, `StreamTimeout` or `StreamRefused` |

Any other answer resolves or rejects the promise. The proxy abandons an attempt it repeats: it
aborts the axios request or cancels the stream, and only the last attempt can settle the promise. A
repeat over muxws waits for the peer to reconnect.

```ts
route_rest(ItemViewSet, {
  basePath: '/items',
  pkFieldName: 'id',
  attemptTimeoutMs: 30_000,
  repeat: { withinMs: 600_000, delayMs: 1000 },
});
```

| Option | Default | Effect |
|---|---|---|
| `attemptTimeoutMs` | `30000` | timeout of one attempt over REST; over muxws `timeoutMs` already is per attempt |
| `repeat.withinMs` | `600000` | the proxy repeats until this long after the first attempt, then rejects with the last error; `0` turns repeating off |
| `repeat.delayMs` | `1000` | delay before a repeat after a network error or a gateway status, doubled after each, at most 30 s; a repeat after the attempt's own timeout is sent at once, since the server held that attempt attached |

The default `withinMs` equals the default `viewsets_idempotency_lock_ttl`.

## taskwire

taskwire takes the key as the token of an operation, so every call made through a proxy can be
tracked without a separate token:

- the frontend registers a handler on the promise's `key`, after the call has been sent; the handler
  is removed on `done`, `failed` or `cancelled`;
- the backend writes `queued` when the call arrives, and `running`, `done` or `failed` where the
  action runs: in the web process, or in the Celery worker.

These parts are specified in taskwire, not here.

## Changes elsewhere in fastapi-viewsets

- `process_command` runs the request as a task of its own. When the stream ends before the answer
  (the peer cancels it or the socket is lost), the request continues and its answer is discarded,
  as over HTTP. Cancelling an operation is done through the application (taskwire's cancel), not by
  cancelling the stream.
- `MuxwsProxyImpl.request()` opens its stream synchronously once the peer has resolved, instead of
  after `await this.peer()`. Anything that binds state for the synchronous part of a call (taskwire's
  `run()`) then reaches the stream.
- `RequestOptions` gains `key`.
- The proxies' public methods return `ViewSetCall<R>` instead of `Promise<R>`. The type is a
  subtype, so existing callers are unaffected.
