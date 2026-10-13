# route_muxws — API Reference

```ts
import { route_muxws, MuxwsProxyImpl } from '@dynamicforms/fastapi-viewsets';
```

The muxws counterpart of [`route_rest`](./route-rest): the same ViewSet surface, sent over one
WebSocket. A factory-built ViewSet for muxws is [`muxwsViewSet`](./viewset-factory); the transport
itself is described in [muxws](../guide/muxws).

## Signature

```ts
function route_muxws<M>(viewSetClass: ViewSetClass, options: MuxwsProxyOptions): MuxwsProxy<M>
```

`viewSetClass` is used for its `static declares` only, which is handed to the proxy for the schema
check. `MuxwsProxy<M>` is `M`: an instance of `MuxwsProxyImpl` cast to it, unchecked, as
[`route_rest`'s return value](./route-rest#return-value) is. Passing a factory-built class fails to
compile.

## MuxwsProxyOptions

```ts
interface MuxwsProxyOptions {
  basePath: string;
  pkFieldName: string;
  /** Set false to skip the startup schema check (advisory, costs one request). */
  validateSchema?: boolean;
  /** The mixins the ViewSet declares, for the schema check. */
  declares?: readonly ViewSetMixinDeclaration[];
  peer: MuxwsPeerSource;
  /** Per-request timeout in milliseconds, the wait for the reply headers included. */
  timeoutMs?: number;
  /** Headers added to every request this proxy makes, over the handshake's own. */
  headers?: Record<string, string>;
}
```

| Option | Description |
|--------|-------------|
| `peer` | A muxws `Peer`, or a function returning one or a promise of one; the function is called once and its result cached |
| `timeoutMs` | Bounds a call from opening the stream to the last byte of the reply; muxws resets the stream with `TIMEOUT` and the call rejects with its `StreamTimeout` |
| `headers` | Sent on every call; a pseudo-header (`:method`, `:path`, `:query`) cannot be overridden |

## Peer types

`muxws` is an optional peer dependency, so the proxy types the parts of it it uses structurally:

```ts
interface MuxwsStreamLike {
  result(options?: { timeoutMs?: number }): Promise<unknown>;
  readonly replyHeadersArrived: Promise<void>;
  readonly replyHeaders: Record<string, unknown> | null | undefined;
}

interface MuxwsPeerLike {
  open(payload?: unknown, options?: { headers?: Record<string, unknown>; end?: boolean }): MuxwsStreamLike;
}

type MuxwsPeerSource = MuxwsPeerLike | (() => MuxwsPeerLike | Promise<MuxwsPeerLike>);

type MuxwsProxy<M> = M;
```

A muxws `Peer` satisfies `MuxwsPeerLike`.

## MuxwsProxyImpl

The concrete class behind every muxws proxy and every `muxwsViewSet` class. Each call opens one
stream with `:method`, `:path` and `:query` pseudo-headers and the request body as payload, and
reads the status from the reply's `:status` header (200 when absent). A status of 400 or above
throws a `ViewSetRequestError`. The HTTP mapping is [`route_rest`'s](./route-rest#http-mapping).

## ViewSetProxyBase

`RestProxyImpl` and `MuxwsProxyImpl` both extend `ViewSetProxyBase<K, T, PK>`, which implements
every standard action against the abstract `request()`. A transport of one's own extends it,
implements `request()`, and calls `initSchemaValidation()` as the last statement of its constructor.
