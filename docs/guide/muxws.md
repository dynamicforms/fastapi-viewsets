# muxws transport

A viewset registered with `route_viewset` can also be reached over a single WebSocket, using
[muxws](https://docs.velis.si/muxws) — a library that gives HTTP/2 stream semantics over one
WebSocket connection.

There is no second implementation to keep in step: a command goes through the application that
serves the WebSocket endpoint, as an HTTP request does. Its middleware, its exception handlers, its
router and the dependencies declared on it, `app.state`, the lifespan state and
`app.dependency_overrides` apply to both transports, and so does everything a route declares —
validation, dependencies, command middleware, context processors and response models. An endpoint
does not know which transport a request arrived over.

muxws is a transport only: it turns a stream into an ASGI request, passes it to the application and
sends the response back as the reply. See [What the transport does](#what-the-transport-does).

Each viewset endpoint is one route on the router you hand to `route_viewset`, whichever transports
publish it. A route that is not published on a request's transport does not match it, and routing
carries on as if the route were not there (see [Choosing which viewsets are published](#choosing-which-viewsets-are-published)).

## Installation

```bash
pip install "dynamicforms-fastapi-viewsets[muxws]"
npm install muxws
```

Requires muxws 0.4.0 or later in Python and 0.4.1 or later on npm: response headers on data frames,
and open interceptors, whose headers a repeated call keeps.

## Server

muxws performs the WebSocket upgrade itself, because only it knows which `muxws.v1.<codec>`
subprotocol to select. **Do not call `websocket.accept()`** — accepting first raises a
`ProtocolError`.

```python
from fastapi import FastAPI, WebSocket
from muxws import accept, Stream

from fastapi_viewsets.mux_ws import process_command

app = FastAPI()
ALLOWED_ORIGINS = ["https://app.example"]

@app.websocket("/ws")
async def muxws_endpoint(websocket: WebSocket) -> None:
    peer = await accept(websocket, allowed_origins=ALLOWED_ORIGINS)
    peer.on_stream(lambda payload, stream: process_command(payload, stream, connection=websocket, peer=peer))
    await peer.serve()
```

`peer=` places the peer in each command's request scope as `request.scope["fastapi_viewsets.peer"]`
(`fastapi_viewsets.routing.PEER_SCOPE_KEY`), `None` without it. An endpoint that addresses the
socket itself reads it from there; the [events](./events#peer) viewset's `subscribe` and `unsubscribe` require it.

A peer may have only one `on_stream` handler, so this library never installs one itself — you keep
ownership of the handler and call `process_command` from it. It returns `False` for a stream that
is not a viewset command, which is how viewset traffic and your own protocol share one socket:

```python
async def on_stream(payload, stream: Stream) -> None:
    if await process_command(payload, stream, connection=websocket, peer=peer):
        return
    await my_own_dispatch(payload, stream)
```

`process_command` passes a command to the application that serves the WebSocket endpoint,
`connection.scope["app"]`. `app=` passes it to another application instead, which a test without a
WebSocket uses; a command with neither `connection` nor `app` raises `ValueError`.

A command's `:path` is the path the application routes, as for an HTTP request to it: a router
included with `app.include_router(router, prefix="/api")` serves `/api/music`, over REST and over
muxws alike. A client therefore uses one `basePath` for both transports.

## What the transport does

The transport builds an ASGI `http` request from a command and sends the application's response
back as the reply:

| Request | Built from |
|---------|------------|
| method, path, query string | `:method`, `:path`, `:query` |
| headers | the WebSocket handshake's headers, then `base_headers`, then the headers stated on the stream; see [Authentication](#authentication) |
| body | the stream's payload, re-encoded as JSON |
| `request.client`, `request.server` | the WebSocket's addresses |
| `request.state` | a shallow copy of the lifespan state, as an HTTP server makes for each request |
| `scope["fastapi_viewsets.transport"]` | `"muxws"`; a request without it is a REST request |
| `scope["fastapi_viewsets.peer"]` | the `peer` argument of `process_command`, `None` without it |

The reply carries the response's status and headers, without the headers that describe an HTTP
message (`content-length`, `transfer-encoding`, `connection`, `keep-alive`), and the body decoded
from JSON.

An exception the application raises after it has started its response — Starlette's
`ServerErrorMiddleware` answers it with a 500, through the application's own handler for
`Exception` where there is one, and re-raises — is logged, and that response is the reply. An
exception raised before any response is logged and answered with a 500 without a body.

Two behaviours of HTTP middleware have no counterpart on a WebSocket:

- **Compression.** The handshake's `accept-encoding` is not passed on, so a compression middleware
  (`GZipMiddleware`) leaves the reply uncompressed; muxws carries it as a decoded payload. A
  command that states `accept-encoding` itself receives a compressed body.
- **Cookies.** A `set-cookie` header reaches the client in the reply headers, but a browser does not
  set a cookie from a WebSocket message. A cookie the browser has to store is set over HTTP. The
  reply headers hold one value per header name, the last one the response sent, so of a response
  that sets several cookies the reply carries the last `set-cookie`.

### A stream that ends early

`process_command` runs the request as a task of its own. When the stream ends before the answer —
the peer cancels it or the socket is lost, and muxws cancels the stream handler — the request runs
to completion and its answer is discarded, as an HTTP server lets a request run after its client
disconnected. An exception the request raises after that is logged. Cancelling the stream does not
stop the action; an action that has to be stoppable is stopped through the application. A client
that repeats the call with the same `Idempotency-Key` receives the outcome of the first one (see
[Idempotent Calls](./idempotency)).

## Client

A ViewSet is a class built by `muxwsViewSet`, given the name of the pk field and the mixins the BE
viewset is made of:

```ts
import { connect } from 'muxws';
import {
  muxwsViewSet, BulkViewSetMixin, LookupMixin, PaginatedListMixin,
  CursorListMixin, ReadOnlyViewSetMixin, RetrieveMixin,
} from '@dynamicforms/fastapi-viewsets';

class TrackViewSet extends muxwsViewSet<Track>()('id', [BulkViewSetMixin, PaginatedListMixin, LookupMixin]) {
  /** A custom endpoint, written against request() so the same body works on either transport. */
  async count(): Promise<number> {
    return this.request<number>('GET', '/count');
  }
}

const peer = await connect('ws://localhost:8000/ws');
const tracks = new TrackViewSet({ basePath: '/music', peer });

const page = await tracks.listPage({ offset: 0, limit: 50 });
const total = await tracks.count();
```

The mixin list is written once, as values, and does three jobs: it decides which actions the class
exposes to callers, it types them, and it is handed to the proxy so the startup schema check can
compare it against `GET /music/schema`. Calling an action the ViewSet did not declare is a compile
error (TS2339) rather than a 404 at runtime.

The empty `()` is required. TypeScript has no partial type-argument inference, so `Track` cannot be
given explicitly while the pk field and the mixin list are inferred from arguments of the same call
(TS2558).

`'id'` is an argument, not a type argument: it is checked against `Track`'s fields — a name that is
not a field, or a field that cannot be a key, is TS2345 — and the pk type of `retrieve` and
`destroy` is read off `Track['id']`. It is not repeated in the constructor options, which are
`{ basePath, peer, validateSchema?, timeoutMs?, headers?, repeat?, events? }`; `pkFieldName` and `declares`
are bound by the factory and are TS2353 if passed.

Inside the class body, `this.request(method, path, options?)` and `this.basePath` are reachable.
They are protected, so a caller cannot touch them (TS2445).

To narrow a ViewSet — the same model, served by a BE viewset built from fewer mixins — call the
factory again with the smaller list:

```ts
class TrackDbViewSet extends muxwsViewSet<Track>()('id', [CursorListMixin, RetrieveMixin]) {}
```

A subclass cannot restate `static declares` instead: the type surface still comes from the factory
call above it, and the restated list is checked against the one the factory was given (TS2417).

A declared action can be overridden with a method, `super` included:

```ts
class CachedTrackViewSet extends muxwsViewSet<Track>()('id', [ReadOnlyViewSetMixin]) {
  private cache?: Track[];

  override async list(): Promise<Track[]> {
    return (this.cache ??= await super.list());
  }
}
```

A ViewSet speaks one transport. To offer both, build both — `restViewSet(...)` and
`muxwsViewSet(...)` — and pick between them; they share nothing but the model and the mixin list,
and a custom endpoint written against `request()` works unchanged on either.

Because the ViewSet is usually constructed at module scope, before `connect()` has resolved, `peer`
also accepts a function. It is called once and its result cached: a muxws Peer survives its own
reconnects, so there is nothing to re-resolve.

Each call opens one stream, with the call's `Idempotency-Key` among its headers (see
[Idempotent Calls](./idempotency#frontend)). Once the peer has resolved, the stream of a call's first
attempt is opened synchronously, inside the method call: whatever the application binds for the
synchronous part of the call, and a muxws open interceptor reads, reaches that stream. The first
call made while a function `peer` is still resolving, and every repeat, open their streams later; a
repeat is opened with the headers of the call's first stream, those the interceptors added included.

```ts
import { connect, type Peer } from 'muxws';

let peerPromise: Promise<Peer> | undefined;
const peer = () => (peerPromise ??= connect('ws://localhost:8000/ws'));

const tracks = new TrackViewSet({ basePath: '/music', peer });   // constructed at module scope
```

### route_muxws

The older form. It takes the ViewSet class as a type token and returns a bare proxy, cast to the
mixin interface named as `M`:

```ts
import { route_muxws, BulkViewSetMixin, LookupMixin, PaginatedListMixin } from '@dynamicforms/fastapi-viewsets';

class TrackApi extends BulkViewSetMixin<number, Track, 'id'> {
  static declares = [BulkViewSetMixin, PaginatedListMixin, LookupMixin];
}

const tracks = route_muxws<BulkViewSetMixin<number, Track, 'id'> & PaginatedListMixin<Track>>(TrackApi, {
  basePath: '/music',
  pkFieldName: 'id',
  peer,
});
```

It is still supported, and its limitation is worth knowing: the object is that bare proxy rather
than your own class, so a custom endpoint named in `M` type-checks and is `undefined` when called.

## Choosing which viewsets are published

Three levels, each able to defer to the next by leaving the answer `None`:

```python
from fastapi_viewsets.conf import settings
from fastapi_viewsets.mux_ws import transports

settings.viewsets_register_muxws = True          # global default

@route_viewset(router, base_path="/music", register_muxws=False)   # per viewset
class MusicViewSet(...):
    __router = APIRouter()

    @transports(rest=False)                      # per endpoint: muxws only
    @__router.get("live")
    async def live(self, context: Context) -> list[Track]: ...
```

`@transports` goes *outside* the router decorator — the router captures the function at decoration
time and `transports` only marks it. `rest` is a plain bool, since REST is the baseline; `muxws` is
tri-state so that marking one endpoint does not silently opt its whole viewset in or out.

The global setting is read at decoration time, so set it before any viewset class is decorated.

Every endpoint is one route on `router`, whatever the answer: a route matches only requests arriving
over the transports it is published on, and a request over any other transport goes on to the next
route. A muxws-only `GET /music/live` therefore does not stand in the way of a REST
`GET /music/{pk}`. A route not published on REST is left out of the application's OpenAPI document.

## Schema

`GET /{base_path}/schema` answers over both transports, and each reports the endpoints reachable
over the transport it was asked on. When an endpoint is published on only one of them, the two
schemas differ. Both documents list `/schema` itself. Their paths start at `base_path`, without the
prefix the router is included under (`include_router(prefix=...)`) and with path parameters
unsubstituted.

## Python reference

`fastapi_viewsets.mux_ws` exports, besides `process_command` and `transports`:

| Name | Description |
|------|-------------|
| `schema_for(base_path)` | The OpenAPI document of the viewset registered at `base_path` as reachable over muxws, or `None` |
| `registered_viewsets()` | The registered viewset classes mapped to their base paths |
| `is_registered(cls)` | Whether `cls` is registered on muxws |
| `register_viewset(cls, base_path, route_kwargs, tags=None)` | Records a viewset's muxws-reachable endpoints; `route_viewset` calls it. Re-registering a class replaces its registration |
| `reset_registry()` | Drops every registration; for tests |
| `resolve_register_muxws(endpoint, viewset_level)` | Whether an endpoint is on muxws: the endpoint's `@transports`, else `viewset_level`, else `settings.viewsets_register_muxws` |
| `resolve_register_rest(endpoint, viewset_level=None)` | Whether an endpoint is on REST: the endpoint's `@transports`, else `viewset_level`, else `True` |
| `METHOD_KEY`, `PATH_KEY`, `QUERY_KEY`, `STATUS_KEY` | `":method"`, `":path"`, `":query"`, `":status"` |

`fastapi_viewsets.routing` holds the routing both transports share:

| Name | Description |
|------|-------------|
| `TransportRoute` | An `APIRoute` that matches only requests arriving over one of its `transports` (a `frozenset` of `"rest"`, `"muxws"`), and answers every error of its request with the [error body](./error-codes#the-routes-of-a-viewset) |
| `transport_route_class(base, transports)` | The route class `route_viewset` registers an endpoint with: the router's `route_class` restricted to `transports` |
| `request_transport(scope)` | The transport a request arrived over: `scope["fastapi_viewsets.transport"]`, or `"rest"` without it |
| `TRANSPORT_SCOPE_KEY`, `REST`, `MUXWS` | `"fastapi_viewsets.transport"`, `"rest"`, `"muxws"` |
| `PEER_SCOPE_KEY` | `"fastapi_viewsets.peer"`, the scope key of the `peer` argument of `process_command`; absent from a REST request |
| `GATE_RUN_SCOPE_KEY` | `"fastapi_viewsets.events.gate"`, present on the request of an events [gate run](./events#the-gate-run); re-exported from `fastapi_viewsets.events` |
| `openapi_endpoint(route_kwargs)` | A `/schema` endpoint answering with the OpenAPI document of the routes listed in `add_api_route` form |

## The wire format

muxws does no routing of its own — its specification forbids looking at the opening payload to pick
a handler — so the addressing is this library's. It mirrors HTTP/2: an `open` frame carries
`headers` plus a `payload`, as HTTP/2 carries HEADERS followed by DATA.

```js
peer.open(
  { title: 'Kind of Blue' },          // the request body
  { headers: {
      ':method': 'POST',
      ':path': '/music',
      authorization: 'Bearer ...',    // anything not :-prefixed is an ordinary HTTP header
  }},
);
```

The `:` prefix is what keeps method and path from colliding with real headers, since an HTTP field
name may not contain a colon.

`:query` accepts a pre-encoded string or a mapping. A mapping is easier to write, and a list value
becomes a repeated key (`?genre=jazz&genre=blues`), which is how FastAPI binds a `list[str]`.

### Responses

The response status is announced **before** the body, in the answering side's leading headers —
exactly as HTTP/2 puts `:status` in HEADERS ahead of DATA:

```json
{"type": "data", "stream": 3, "end": true, "headers": {":status": 200}, "payload": [...]}
```

muxws carries `headers` on the first `data` frame a peer sends as well as on `open` (0.3.1+, SPEC
WSM-FRM-016). The client reads them from `stream.replyHeaders` once `stream.replyHeadersArrived`
resolves — `reply_headers` / `reply_headers_arrived` in Python — so the status is known before the
body, for a streaming reply as much as a unary one.

### Errors

A 404 or a 422 comes back as a normal reply carrying that status, with the same
[error body](./error-codes#the-error-body) as over REST. Stream resets are reserved for
transport and protocol failures. A command whose `:method`, `:path` or `:query` is malformed — a
path that is not absolute, a method that is not a string, a `:query` that is neither a string nor a
mapping — is answered with status 400 and `fastapi-viewsets.command_invalid`, `params.reason` naming the
failure. An exception the application raises before it starts a response is answered with 500
`fastapi-viewsets.unhandled_error`, and logged.

On the client that status becomes a `ViewSetRequestError`, whose shape is the one axios raises —
`error.response.status`, `error.response.data` — so error handling written against axios works over
muxws unchanged. See [handling a failed call](./vue-mixins#handling-a-failed-call).

## Authentication

The WebSocket handshake's headers are the baseline every command inherits, without those that
describe the upgrade (`connection`, `upgrade`, `sec-websocket-*`) and without `accept-encoding`. A ViewSet's own
`headers` replace the handshake's value on every call it makes — the client sets them once, per
proxy, not per call:

```ts
new TrackViewSet({
  basePath: '/music',
  peer,
  headers: { authorization: `Bearer ${token}` },   // sent on every call
});
```

Authenticating the connection itself belongs *before* `accept()`, in your own websocket endpoint —
muxws interprets no credential anywhere.

A browser sends the site's cookies with a WebSocket handshake whatever page opened it, so a
cookie-authenticated WebSocket has no CSRF protection. muxws's `accept(websocket, allowed_origins=...)`
checks `Origin` before the upgrade and answers a refused handshake with HTTP 403; `allowed_origins`
takes the exact origins allowed or a callable `(origin: str | None) -> bool` (see
[accept()](https://docs.velis.si/muxws/api/accept)). Without it, `accept()` checks nothing.

The connection also supplies each command's `request.client` and `request.server`: the
WebSocket's own addresses, which [`RateLimiter`](./rate-limiter#identity-key) keys on by default.
`process_command` called without `connection=` gives every command the client `("muxws", 0)`.

## Reconnection

Nothing survives a muxws reconnect: every in-flight stream fails with `ConnectionLost` and the id
space restarts. The proxy waits for `peer.onReconnect` and repeats the call with the same
`Idempotency-Key`; on the server the request of the lost stream runs to completion (see
[A stream that ends early](#a-stream-that-ends-early)), and `Idempotency` answers the repeat with its
outcome. The repeats and their options are described in
[Idempotent Calls](./idempotency#frontend). Hook `peer.onReconnect` to refresh other state when the
socket comes back.

## Performance

Dispatch builds a synthetic ASGI request and calls the application. It costs one JSON
decode/re-encode per call: about 15% of a read, a third of a large write.

The demo — 5000 records, localhost, Python client, so no browser connection limit in play:

| | sequential p50 | sequential p95 | 100 requests at once |
|---|---|---|---|
| REST | 1.02 ms | 1.18 ms | 155.7 ms |
| muxws | 0.44 ms | 0.57 ms | 36.6 ms |

In a browser the burst gap is wider: about six concurrent HTTP/1.1 connections are allowed per
host, so a seventh request waits, while muxws multiplexes every call onto one socket.
