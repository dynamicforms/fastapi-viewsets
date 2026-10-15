# Idempotent calls

Status: concept. Nothing described here is implemented yet.

## Purpose

A call whose response is lost (a timeout, a dropped socket, a phone losing signal) leaves the
client not knowing whether the action ran. Repeating a `POST` can create a second record; not
repeating it can lose the first one. This API makes every call repeatable: the client sends a key
with each call and repeats the call with the same key, and the server answers the repeat with the
outcome of the first call instead of running the action again.

The same mechanism lets a call answer before its action completes. The server answers `202`, the
action continues, and the client collects the outcome by repeating the call. A REST call therefore
does not depend on HTTP timeouts along the way, and the promise on the frontend resolves to the same
body a direct answer would have carried.

The API follows two specifications:

- [The Idempotency-Key HTTP Header Field](https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-idempotency-key-header)
  (`draft-ietf-httpapi-idempotency-key-header-07`, an Internet-Draft): the key, the repeat, `409`
  and `422`;
- [RFC 7240, Prefer Header for HTTP](https://www.rfc-editor.org/rfc/rfc7240): `respond-async` and
  `wait`, with which the client asks for `202` after a time it chooses.

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

1. **The key identifies one call.** The frontend generates a new key for every call and sends the
   same key only when it repeats that call.
2. **Unsafe methods are deduplicated.** For `POST`, `PUT`, `PATCH` and `DELETE` the server keeps a
   record per key. For `GET` and `HEAD` it keeps none: a repeated `GET` runs again, which is its
   correct answer.
3. **A record exists from the moment the action starts.** A call rejected before the action
   (authentication, a `depends()` rejection, a `422` from request validation, rate limiting) leaves
   no record, and its repeat is a new call.
4. **A repeat of a finished call answers with the stored outcome.** Status, headers and body are
   those of the first answer. Error outcomes are stored too: an action that ran and failed has run.
5. **A repeat of a running call answers `409`** (draft). The frontend repeats again
   after a delay.
6. **A key reused with a different fingerprint answers `422`** (draft).
7. **A record expires `ttl` after the outcome is stored.** A repeat after that is a new call.
8. **A running record expires `lock_ttl` after it was written** unless the action finishes first.
   A process that dies mid-action leaves a running record; once it expires, a repeat runs the
   action again.
9. **`Prefer: respond-async, wait=N` lets the server answer `202` after `N` seconds.** An action
   that completes within `N` seconds answers normally. One that does not answers `202` with
   `Preference-Applied: respond-async`, continues, and stores its outcome under the key. Without the
   preference the server never answers `202`.

## Backend

### The middleware

```python
from fastapi_viewsets.middleware.idempotency import Idempotency

settings.viewsets_command_middleware = [
    Idempotency(redis_client=redis, ttl=24 * 3600, lock_ttl=600),
    ...
]
```

`Idempotency` is a command middleware, so it runs on REST and muxws alike. It is placed first in
`viewsets_command_middleware`: a middleware ahead of it would run again on every repeat. Without
`redis_client` the records live in the process, which suits a single process; with one they live in
Redis, which a repeat landing on another process needs.

| Call | Record for the key | Answer |
|---|---|---|
| no `Idempotency-Key`, or a safe method | — | the action runs; nothing is stored |
| key | none | the record is written as running (atomic `SET NX`), the action runs, the outcome is stored |
| key, same fingerprint | running | `409`, `detail_code` `idempotency_in_progress` |
| key, same fingerprint | outcome | the stored outcome |
| key, different fingerprint | any | `422`, `detail_code` `idempotency_key_reused` |
| key that is not a Structured Field string | — | `400`, `detail_code` `idempotency_key_invalid` |

The errors are `DfViewSetError`s, with `detail_code` and `detail_params` like every other error of
the library (see [Error codes](docs/guide/error-codes.md)).

The stored outcome is the `ViewSetResult` of the chain below the middleware: body, status code,
headers and cookies. A repeat returns it as its own `ViewSetResult`, so FastAPI serialises it
through the same response model as the first answer.

### Answering before the action completes

With `Prefer: respond-async, wait=N` on a call with a key, the middleware runs the rest of the
chain as a task and waits for it at most `N` seconds. An action that completes in time answers
normally. Otherwise the middleware answers `202` with `Preference-Applied: respond-async` and an
empty body, the task continues, and its outcome is stored under the key when it completes.

For a `celery_viewset_client` action the task is the wait for the worker's result; the work itself
is already in the worker. For an action that runs in the web process the task is the action itself,
running after the request has been answered. Two things have ended by then:

- dependencies with `yield` on the route have exited (FastAPI exits them once the response is sent);
- for `singleton` and `instance-key` viewsets, `save_state()` runs when the chain returns, which is
  when `202` is answered, not when the action completes.

See "Open questions".

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

The proxy repeats a call, with the same key, when it received no answer:

| Transport | No answer |
|---|---|
| REST | an axios error without `response` (network error, timeout) |
| muxws | the stream ended with `ConnectionLost`, `StreamTimeout` or `StreamRefused` |

It also repeats on `409` and, after a `202`, until the answer is neither `202` nor `409`. Any other
answer resolves or rejects the promise. A repeat over muxws waits for the peer to reconnect.

```ts
route_rest(ItemViewSet, {
  basePath: '/items',
  pkFieldName: 'id',
  repeat: { attempts: 5, delayMs: 1000 },
  respondAsyncAfter: 20,   // seconds; sends `Prefer: respond-async, wait=20`
});
```

| Option | Default | Effect |
|---|---|---|
| `repeat.attempts` | `5` | repeats of one call after no answer; `0` turns repeating off |
| `repeat.delayMs` | `1000` | delay before a repeat after no answer or `409`, doubled after each, at most 30 s |
| `respondAsyncAfter` | `undefined` | seconds sent as `wait`; without it no `Prefer` header is sent |
| `awaitOutcome` | `undefined` | `(key) => Promise<void>`, called after `202`; the proxy repeats the call when it resolves instead of after `delayMs` |

`awaitOutcome` is the place where something that learns of the outcome sooner than a poll would
(taskwire's push) tells the proxy to collect it.

## taskwire

taskwire takes the key as the token of an operation, so every call made through a proxy can be
tracked without a separate token:

- the frontend registers a handler on the promise's `key`, after the call has been sent; the handler
  is removed on `done`, `failed` or `cancelled`;
- the backend writes `queued` when the call arrives, and `running`, `done` or `failed` where the
  action runs: in the web process, or in the Celery worker;
- `awaitOutcome` resolves when taskwire reports a terminal state, so a deferred call is collected
  without polling.

These parts are specified in taskwire, not here.

## Changes elsewhere in fastapi-viewsets

- `MuxwsProxyImpl.request()` opens its stream synchronously once the peer has resolved, instead of
  after `await this.peer()`. Anything that binds state for the synchronous part of a call (taskwire's
  `run()`) then reaches the stream.
- `RequestOptions` gains `key`.
- The proxies' public methods return `ViewSetCall<R>` instead of `Promise<R>`. The type is a
  subtype, so existing callers are unaffected.

## Open questions

- Whether a call with `Prefer: respond-async` to an action running in the web process is allowed at
  all, given the two things that have ended when `202` is answered; or allowed only for
  `per-request` viewsets; or run under a `lifecycle_runner` that defers `save_state()` to the end of
  the action.
- Whether the proxy sends the key on `GET` and `HEAD`. The server ignores it there (rule 2); sending
  it gives taskwire a token for a slow `GET` without a second header.
- The default `ttl`. The draft leaves it to the server and asks that it be documented.
- Whether a stored outcome keeps `set-cookie`. Repeating it sets the same cookie again, which is what
  the first answer did.
