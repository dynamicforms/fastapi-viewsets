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
check. `MuxwsProxy<M>` is `ViewSetCalls<M>`: `M` with every method returning a
[`ViewSetCall`](./idempotency#frontend), an instance of `MuxwsProxyImpl` cast to it, unchecked, as
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
  /** When a call whose attempt ended without an answer is repeated. */
  repeat?: RepeatOptions;
  peer: MuxwsPeerSource;
  /** Per-attempt timeout in milliseconds, the wait for the reply headers included. */
  timeoutMs?: number;
  /** Headers added to every request this proxy makes, over the handshake's own. */
  headers?: Record<string, string>;
}
```

| Option | Description |
|--------|-------------|
| `peer` | A muxws `Peer`, or a function returning one or a promise of one; the function is called once and its result cached |
| `timeoutMs` | Bounds an attempt from opening the stream to the last byte of the reply; muxws resets the stream with `TIMEOUT` and the call is repeated at once, or rejects with that `StreamTimeout` when it is not repeated. Unset, an attempt has no timeout |
| `headers` | Sent on every call; a pseudo-header (`:method`, `:path`, `:query`) and `Idempotency-Key` (in any letter case) cannot be overridden |
| `repeat` | [`RepeatOptions`](./idempotency#repeatoptions); default `{ withinMs: 600000, delayMs: 1000 }` |

## Peer types

`muxws` is an optional peer dependency, so the proxy types the parts of it it uses structurally:

```ts
interface MuxwsStreamLike {
  result(options?: { timeoutMs?: number }): Promise<unknown>;
  readonly replyHeadersArrived: Promise<void>;
  readonly replyHeaders: Record<string, unknown> | null | undefined;
  /** The headers the stream was opened with, after the peer's open interceptors. */
  readonly headers?: Record<string, unknown>;
  /** Resets the stream with CANCELLED; called on an attempt the proxy repeats. */
  cancel?(reason?: string): Promise<void>;
}

interface MuxwsPeerLike {
  open(payload?: unknown, options?: { headers?: Record<string, unknown>; end?: boolean }): MuxwsStreamLike;
  readonly isOpen?: boolean;
  onReconnect?(handler: (...args: any[]) => void): void;
  onClose?(handler: (reason: { readonly willRetry: boolean }) => void): void;
}

type MuxwsPeerSource = MuxwsPeerLike | (() => MuxwsPeerLike | Promise<MuxwsPeerLike>);

type MuxwsProxy<M> = ViewSetCalls<M>;
```

A muxws `Peer` satisfies `MuxwsPeerLike`. A repeat after a lost connection waits for the peer while
`isOpen` is `false`, until `onReconnect` or a final `onClose` (`willRetry` false) fires; the proxy
registers one handler of each per peer. A peer without `isOpen` or `onReconnect` is not waited for.

## MuxwsProxyImpl

The concrete class behind every muxws proxy and every `muxwsViewSet` class. Each attempt of a call
opens one stream with `:method`, `:path` and `:query` pseudo-headers, the proxy's `headers`
overridden by the call's `RequestOptions.headers`, the call's `Idempotency-Key`, and the request
body as payload, and reads the status from the reply's `:status` header (200 when absent). A status
of 400 or above throws a `ViewSetRequestError`. The HTTP mapping is
[`route_rest`'s](./route-rest#http-mapping).

Once the peer has resolved, `request()` opens the stream of the first attempt synchronously. An
attempt that fails with `ConnectionLost`, `StreamTimeout` or `StreamRefused`, or whose `open()` throws
`ConnectionClosed` (the peer is between sockets) or `ConnectionGoingAway` (its connection drains after
a goaway) while the peer has not closed for good, is repeated as described in
[Idempotent Calls](../guide/idempotency#repeats); its stream is cancelled first. A repeat is opened
with the `headers` of the call's first stream, those the peer's open interceptors added included; a
stream without `headers` leaves the repeat with the headers the proxy built.

## ViewSetProxyBase

`RestProxyImpl` and `MuxwsProxyImpl` both extend `ViewSetProxyBase<K, T, PK>`, which implements
every standard action against the abstract `request()`. A transport of one's own extends it,
implements `request()`, and calls `initSchemaValidation()` as the last statement of its constructor.

`request()` returns a `ViewSetCall<R>`: it takes the key from `options.key` or `newCallKey()`, sends
it as `Idempotency-Key: idempotencyKeyHeader(key)`, and returns `issueCall(promise, key)`, called
synchronously. `issueCall` is what gives the promise of the method that called `request()` its
`key`. The constructor of `ViewSetProxyBase` binds every method of the instance's class chain, other
than `constructor` and `request`, as an own property that sets that `key`.
