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
- **Outcome.** The `ViewSetResult` of the chain below the middleware: body, headers, cookies and
  status code. A repeat returns a copy of it, and FastAPI serialises it through the same response
  model as the first answer.
- **Error outcomes are stored too.** An `HTTPException` is raised again on a repeat with its status
  code, `detail` and headers, a `DfViewSetError` with its `code` and `params`. A
  `RequestValidationError` raised by the action is replayed as a `422` with its errors as `detail`.
  Any other exception is replayed as a `500` with `detail` `"Internal Server Error"`.
- **What is stored depends on the method.**
  - `POST`, `PUT`, `PATCH`, `DELETE`: the outcome is stored.
  - `GET`, `HEAD`: the outcome is stored only when a repeat attached to the call while it ran. A
    repeat of a finished `GET` whose outcome was not stored runs again.
  - An outcome with a cookie or a `set-cookie` header is not stored, whatever the method: its record
    is deleted when the action completes, and a repeat runs the action again, an attached repeat
    included.
- **A stored outcome expires** `settings.viewsets_idempotency_ttl` seconds after it was stored.
- **A running record expires** `settings.viewsets_idempotency_lock_ttl` seconds after it was written,
  unless the action finishes first. A process that dies mid-action leaves a running record; once it
  expires, a repeat runs the action again, and so does a repeat attached to it at that moment. The
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
| `<key_prefix><key>:attached` | Set by a repeat on another process that attached to a running `GET` or `HEAD` |
| channel `<key_prefix><key>` | A message is published when the call completes |

- The running record is written with `SET NX PX` and the lock TTL in milliseconds.
- On completion one Lua script replaces the running record with the outcome (with the TTL in
  milliseconds) or deletes it, provided it is still the record this process wrote. The process then
  publishes on the key's channel and resolves the futures of the repeats it holds, in that order.
- A repeat on the process running the call awaits that call's future. A repeat on another process
  sets the `:attached` key, subscribes to the channel, reads the record again (an outcome stored
  before the subscription) and waits for the message or for the running record to expire.

## Known limitations

- Records are not scoped to a caller: a key is identified by its value alone. Two callers that send
  the same key and the same request share one record. A client-generated UUID makes this unlikely.
- In-memory storage (the default) serves one process; a repeat that reaches another process runs the
  action again.
- A repeat of a call that raised an exception other than `HTTPException` answers `{"detail":
  "Internal Server Error"}` through FastAPI's `HTTPException` handling, not through the application's
  handler for `Exception`.
