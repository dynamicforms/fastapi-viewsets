/**
 * muxws proxy for ViewSets: the route_rest surface over one WebSocket. A proxy speaks one transport;
 * for both, create a REST and a muxws proxy for the same ViewSet.
 */

import {
  type AttemptFailure,
  type CallAttempt,
  issueCall,
  newCallKey,
  runCall,
  type ViewSetCall,
  type ViewSetCalls,
  withKeyHeader,
} from './call';
import type { KeyOf, RecordOf, ViewSetWatch } from './events/changes';
import { type EventClient, eventsOf } from './events/client';
import type { DefaultSortOf, KeyType } from './mixins';
import { readHeaders, readStatus } from './muxws-reply';
import {
  FACTORY_BUILT,
  type HttpMethod,
  type ProxyBaseOptions,
  type QueryParams,
  type RequestOptions,
  ViewSetProxyBase,
  type ViewSetMixinDeclaration,
  ViewSetRequestError,
} from './proxy-base';

/** The parts of a muxws Stream this proxy uses, typed structurally so `muxws` stays an optional dependency. */
export interface MuxwsStreamLike {
  result(options?: { timeoutMs?: number }): Promise<unknown>;
  readonly replyHeadersArrived: Promise<void>;
  readonly replyHeaders: Record<string, unknown> | null | undefined;
  /** The headers the stream was opened with, after the peer's open interceptors. */
  readonly headers?: Record<string, unknown>;
  /** Resets the stream with CANCELLED; called on an attempt the proxy repeats. */
  cancel?(reason?: string): Promise<void>;
}

/**
 * The parts of a muxws Peer this proxy uses. A repeat waits for a reconnect through `isOpen`, `onReconnect`
 * and `onClose`; without them it opens once its delay has passed.
 */
export interface MuxwsPeerLike {
  open(payload?: unknown, options?: { headers?: Record<string, unknown>; end?: boolean }): MuxwsStreamLike;
  readonly isOpen?: boolean;
  /** True once the peer has closed for good. */
  readonly isClosed?: boolean;
  onReconnect?(handler: (...args: any[]) => void): void;
  onClose?(handler: (reason: { readonly willRetry: boolean }) => void): void;
}

/**
 * A peer, or a function returning one (for a proxy built before `connect()` resolves, or a peer replaced
 * after re-login). The function's result is kept until that peer closes for good, or the function threw
 * or rejected; the next call then calls it again. A call in progress keeps the peer it started on.
 */
export type MuxwsPeerSource = MuxwsPeerLike | (() => MuxwsPeerLike | Promise<MuxwsPeerLike>);

/** `M` with every method returning a `ViewSetCall`, plus `watch()`, and `defaultSort()` when `M` lists. */
export type MuxwsProxy<M> = ViewSetCalls<M> & ViewSetWatch<RecordOf<M>, KeyOf<M>> & DefaultSortOf<keyof M>;

type ViewSetClass = abstract new (...args: any[]) => any;

/** A factory-built class, for the `route_muxws` overload that rejects it (see rest-proxy.ts). */
type FactoryBuiltClass = (abstract new (...args: any[]) => any) & {
  declares: { readonly [FACTORY_BUILT]: any };
};

/** Never assigned; its only use is `typeof FACTORY_BUILT_REJECTION` as a self-describing return type. */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- type-only, read via `typeof` below
declare const FACTORY_BUILT_REJECTION: 'route_muxws cannot take a factory-built class - extend it directly instead';

export interface MuxwsProxyOptions extends ProxyBaseOptions {
  peer: MuxwsPeerSource;
  /**
   * Per-attempt timeout in milliseconds, reply headers included. On expiry muxws resets the stream and
   * the call is repeated (`ProxyBaseOptions.repeat`). Unset: no timeout.
   */
  timeoutMs?: number;
  /**
   * Headers on every request; they override the WebSocket handshake's headers, and
   * `RequestOptions.headers` override them for one call.
   */
  headers?: Record<string, string>;
}

export class MuxwsProxyImpl<K extends KeyType, T, PK extends keyof T> extends ViewSetProxyBase<K, T, PK> {
  private readonly peerSource: MuxwsPeerSource;

  private readonly timeoutMs?: number;

  private readonly headers: Record<string, string>;

  private peerPromise?: Promise<MuxwsPeerLike>;

  /** The peer once it has resolved; `request()` opens on it synchronously. */
  private resolvedPeer?: MuxwsPeerLike;

  constructor(options: MuxwsProxyOptions) {
    super(options);
    this.peerSource = options.peer;
    this.timeoutMs = options.timeoutMs;
    this.headers = options.headers ?? {};
    this.initSchemaValidation();
  }

  /** The source's peer, re-resolved after a function source's peer closed for good or it threw/rejected. */
  private peer(): Promise<MuxwsPeerLike> {
    const source = this.peerSource;
    const held = this.resolvedPeer;
    if (typeof source === 'function' && held && (held.isClosed === true || peerWatches.get(held)?.closed)) {
      this.resolvedPeer = undefined;
      this.peerPromise = undefined;
    }
    if (!this.peerPromise) {
      let result: MuxwsPeerLike | Promise<MuxwsPeerLike>;
      try {
        result = typeof source === 'function' ? source() : source;
      } catch (error) {
        return Promise.reject(error);
      }
      if (isPeer(result)) this.adoptPeer(result);
      const promise: Promise<MuxwsPeerLike> = Promise.resolve(result).then((resolved) =>
        this.peerPromise === promise ? this.adoptPeer(resolved) : resolved,
      );
      this.peerPromise = promise;
      promise.catch(() => {
        if (this.peerPromise === promise) this.peerPromise = undefined;
      });
    }
    return this.peerPromise;
  }

  /**
   * `ProxyBaseOptions.events`, or the client registered for the peer: synchronously once the peer
   * has resolved, as a promise before. Throws, or rejects, for a peer without a client.
   */
  protected override eventClient(): EventClient | Promise<EventClient> {
    if (this.eventClientOption) return this.eventClientOption;
    const peer = this.peer();
    if (this.resolvedPeer) return clientOf(this.resolvedPeer, this.basePath);
    return peer.then((resolved) => clientOf(resolved, this.basePath));
  }

  private adoptPeer(peer: MuxwsPeerLike): MuxwsPeerLike {
    if (this.resolvedPeer !== peer) {
      this.resolvedPeer = peer;
      watchPeer(peer);
    }
    return peer;
  }

  /**
   * Opens a stream with an `Idempotency-Key` and repeats it with the same key and first-stream headers
   * after a timeout, a refusal or a lost connection (waiting for the reconnect), not once the peer has
   * closed for good. With a resolved peer the first stream opens synchronously, so open interceptors
   * see state the caller bound synchronously.
   */
  protected request<R>(method: HttpMethod, path: string, options: RequestOptions = {}): ViewSetCall<R> {
    const key = options.key ?? newCallKey();
    let call: Promise<R>;
    try {
      // HTTP/2-style: `:`-prefixed pseudo-headers for the request line, plain keys for HTTP headers.
      const headers: Record<string, unknown> = {
        ...withKeyHeader({ ...this.headers, ...options.headers }, key),
        ':method': method,
        ':path': `${this.basePath}${path}`,
      };
      if (options.query && Object.keys(options.query).length > 0) headers[':query'] = cleanQuery(options.query);

      // Repeats reuse the first stream's headers (interceptor-added ones included): an interceptor
      // cannot see the caller's synchronously bound state when a repeat opens.
      let sent: Record<string, unknown> | undefined;
      const run = (peer: MuxwsPeerLike): Promise<R> =>
        runCall(
          () => {
            const attempt = this.attempt<R>(peer, options.body, sent ?? headers);
            sent ??= attempt.headers ? { ...attempt.headers } : undefined;
            return attempt;
          },
          (error) => muxwsAttemptFailure(peer, error),
          options.repeat ?? this.repeatOptions,
          (deadline) => waitForOpen(peer, deadline),
          key,
        );
      const peer = this.peer();
      call = this.resolvedPeer ? run(this.resolvedPeer) : peer.then(run);
    } catch (error) {
      call = Promise.reject(error);
    }
    return issueCall(call, key, options.background);
  }

  /** One attempt: one stream, opened synchronously. `headers` are those the stream went out with. */
  private attempt<R>(
    peer: MuxwsPeerLike,
    body: unknown,
    headers: Record<string, unknown>,
  ): CallAttempt<R> & { readonly headers?: Record<string, unknown> } {
    // A unary call: the opening frame is the whole request.
    const stream = peer.open(body, { headers, end: true });
    return {
      headers: stream.headers,
      result: this.answer<R>(stream),
      abandon: () => {
        try {
          void stream.cancel?.('repeated').catch(() => {});
        } catch {
          // a stream that has already closed has nothing to cancel
        }
      },
    };
  }

  private async answer<R>(stream: MuxwsStreamLike): Promise<R> {
    // `result()` starts first so `timeoutMs` also bounds the wait for the reply headers (the status).
    const result = stream.result(this.timeoutMs === undefined ? undefined : { timeoutMs: this.timeoutMs });
    await Promise.race([stream.replyHeadersArrived, result]);
    const status = readStatus(stream.replyHeaders);

    const body = await result;
    if (status >= 400) throw new ViewSetRequestError(status, body, readHeaders(stream.replyHeaders));
    return body as R;
  }
}

function clientOf(peer: MuxwsPeerLike, basePath: string): EventClient {
  const client = eventsOf(peer);
  if (!client) {
    throw new Error(
      `[ViewSet ${basePath}] watch() found no event client for the peer: createEventClient({ peer }) registers one`,
    );
  }
  return client;
}

function isPeer(value: MuxwsPeerLike | Promise<MuxwsPeerLike>): value is MuxwsPeerLike {
  return typeof (value as { then?: unknown }).then !== 'function';
}

/**
 * Classifies muxws errors (by `name`; muxws is not imported) after which no answer arrived.
 * ConnectionClosed / ConnectionGoingAway are repeated only while the peer has not closed for good.
 */
function muxwsAttemptFailure(peer: MuxwsPeerLike, error: unknown): AttemptFailure {
  switch ((error as { name?: unknown } | null)?.name) {
    case 'StreamTimeout':
      return 'timeout';
    case 'ConnectionLost':
    case 'StreamRefused':
      return 'lost';
    case 'ConnectionClosed':
    case 'ConnectionGoingAway':
      return peerWatches.get(peer)?.closed ? null : 'lost';
    default:
      return null;
  }
}

/** Reconnects and the final close of one peer, for the repeats waiting on it. */
interface PeerWatch {
  closed: boolean;
  readonly waiters: Set<() => void>;
}

const peerWatches = new WeakMap<MuxwsPeerLike, PeerWatch>();

/** Subscribes once per peer: muxws has no way to unsubscribe a handler. */
function watchPeer(peer: MuxwsPeerLike): void {
  if (peerWatches.has(peer)) return;
  const watch: PeerWatch = { closed: false, waiters: new Set() };
  peerWatches.set(peer, watch);
  const wake = (): void => {
    for (const waiter of [...watch.waiters]) waiter();
  };
  peer.onReconnect?.(wake);
  peer.onClose?.((reason) => {
    if (reason?.willRetry === false) {
      watch.closed = true;
      wake();
    }
  });
}

/**
 * Resolves when `peer` is open: at once if it is, on its next reconnect or final close otherwise, and
 * at `deadline` at the latest. A peer that does not report `isOpen`, or cannot report a reconnect, is
 * treated as open.
 */
function waitForOpen(peer: MuxwsPeerLike, deadline: number): Promise<void> {
  const watch = peerWatches.get(peer);
  if (peer.isOpen !== false || !watch || !peer.onReconnect || watch.closed) return Promise.resolve();
  return new Promise((resolve) => {
    const done = (): void => {
      clearTimeout(timer);
      watch.waiters.delete(done);
      resolve();
    };
    const timer = setTimeout(done, Math.max(0, deadline - Date.now()));
    watch.waiters.add(done);
  });
}

function cleanQuery(query: QueryParams): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    out[key] = value;
  }
  return out;
}

// Rejects a factory-built class with a message at the call site (see rest-proxy.ts's route_rest).
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- M keeps the call syntax identical to the real overload
function route_muxws<M = never>(
  viewSetClass: FactoryBuiltClass,
  options: MuxwsProxyOptions,
): typeof FACTORY_BUILT_REJECTION;
/** Creates a muxws proxy for the ViewSet class; `M` declares its mixins, as with route_rest. */
function route_muxws<M>(viewSetClass: ViewSetClass, options: MuxwsProxyOptions): MuxwsProxy<M>;
function route_muxws<M>(
  viewSetClass: ViewSetClass,
  options: MuxwsProxyOptions,
): MuxwsProxy<M> | typeof FACTORY_BUILT_REJECTION {
  // `declares` is the only part of the class used at runtime.
  return new MuxwsProxyImpl({
    declares: (viewSetClass as { declares?: ViewSetMixinDeclaration[] }).declares,
    ...options,
  }) as unknown as MuxwsProxy<M>;
}

export { route_muxws };
