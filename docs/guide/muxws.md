# muxws transport

A viewset registered with `route_viewset` can also be reached over a single WebSocket, using
[muxws](https://docs.velis.si/muxws) — a library that gives HTTP/2 stream semantics over one
WebSocket connection.

There is no second implementation to keep in step: a command is dispatched into a FastAPI app built
from the endpoints published on muxws, each registered with the route kwargs `route_viewset` built
for it — the same ones the REST router is given for an endpoint published on both. Validation,
dependencies, command middleware, context processors and response models therefore behave
identically.

That app is the library's own, rebuilt whenever a viewset registers, and not the application object
you created. Middleware installed on yours
(`@app.middleware("http")`, or any ASGI middleware) therefore never sees a command; pass
`app=your_app` to `process_command` when a command has to go through it.

## Installation

```bash
pip install "dynamicforms-fastapi-viewsets[muxws]"
npm install muxws
```

Requires muxws 0.3.1 or later, for response headers on data frames.

## Server

muxws performs the WebSocket upgrade itself, because only it knows which `muxws.v1.<codec>`
subprotocol to select. **Do not call `websocket.accept()`** — accepting first raises a
`ProtocolError`.

```python
from fastapi import FastAPI, WebSocket
from muxws import accept, Stream

from fastapi_viewsets.mux_ws import process_command

app = FastAPI()

@app.websocket("/ws")
async def muxws_endpoint(websocket: WebSocket) -> None:
    peer = await accept(websocket)
    peer.on_stream(lambda payload, stream: process_command(payload, stream, connection=websocket))
    await peer.serve()
```

A peer may have only one `on_stream` handler, so this library never installs one itself — you keep
ownership of the handler and call `process_command` from it. It returns `False` for a stream that
is not a viewset command, which is how viewset traffic and your own protocol share one socket:

```python
async def on_stream(payload, stream: Stream) -> None:
    if await process_command(payload, stream, connection=websocket):
        return
    await my_own_dispatch(payload, stream)
```

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
`{ basePath, peer, validateSchema?, timeoutMs?, headers? }`; `pkFieldName` and `declares` are bound
by the factory and are TS2353 if passed.

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

```ts
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

## Schema

`GET /{base_path}/schema` answers over both transports, and each reports the endpoints reachable
over the transport it was asked on. When an endpoint is published on only one of them, the two
schemas differ.

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

A 404 or a 422 comes back as a normal reply carrying that status. Stream resets are reserved for
transport and protocol failures.

On the client that status becomes a `ViewSetRequestError`, whose shape is the one axios raises —
`error.response.status`, `error.response.data` — so error handling written against axios works over
muxws unchanged. See [handling a failed call](./vue-mixins#handling-a-failed-call).

## Authentication

The WebSocket handshake's headers are the baseline every command inherits. A ViewSet's own
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

## Reconnection

Nothing survives a muxws reconnect: every in-flight stream fails with `ConnectionLost` and the id
space restarts. The proxy propagates that to the caller; it does not retry. Hook `peer.onReconnect`
to refresh state when the socket comes back.

## Performance

Dispatch builds a synthetic ASGI request and calls the dispatch app. It costs one JSON
decode/re-encode per call: about 15% of a read, a third of a large write.

Measured against the demo — 5000 records, localhost, Python client, so no browser connection limit
in play:

| | sequential p50 | sequential p95 | 100 requests at once |
|---|---|---|---|
| REST | 1.02 ms | 1.18 ms | 155.7 ms |
| muxws | 0.44 ms | 0.57 ms | 36.6 ms |

In a browser the burst gap is wider: about six concurrent HTTP/1.1 connections are allowed per
host, so a seventh request waits, while muxws multiplexes every call onto one socket.
