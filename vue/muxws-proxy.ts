/**
 * muxws proxy for ViewSets — the same ViewSet surface as route_rest, sent over one WebSocket.
 *
 *   import { connect } from 'muxws';
 *
 *   const peer = await connect('ws://localhost:8000/ws');
 *   const items = route_muxws<BulkViewSetMixin<number, Item> & LookupMixin>(
 *     ItemViewSet, { basePath: '/items', pkFieldName: 'id', peer },
 *   );
 *   await items.list();
 *
 * A proxy speaks one transport. Creating a REST proxy and a muxws proxy for the same ViewSet is
 * the supported way to have both — they share nothing but the ViewSet's own type.
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
import type { FetchOption, KeyOf, RecordOf, RowOf, ViewSetWatch } from './events/changes';
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

/**
 * The bits of a muxws Peer this proxy uses. Typed structurally rather than by importing the muxws
 * types, so that `muxws` stays an optional dependency: an application that only uses route_rest
 * should not have to install it.
 */
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
 * `isOpen`, `onReconnect` and `onClose` are what a repeat waits on for the peer to reconnect. A peer
 * without them is not waited for: the repeat is opened when its delay has passed.
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
 * Where the peer comes from. A bare peer is the simple case; a function is for the common shape
 * where the proxy is constructed at module scope but `connect()` has not resolved yet, and for an
 * application that replaces its peer (a new `connect()` after logout and login). The function's
 * result is kept while the peer lives — a muxws Peer survives its own reconnects — and the function
 * is called again by the first call after the peer has closed for good (`onClose` with
 * `willRetry: false`, or `isClosed`), and after it threw or its promise rejected. A call in progress
 * keeps the peer it started on. Once the peer has resolved, `request()` opens its stream synchronously.
 */
export type MuxwsPeerSource = MuxwsPeerLike | (() => MuxwsPeerLike | Promise<MuxwsPeerLike>);

/**
 * The mixin interface `M` with every method returning a `ViewSetCall`, `watch()`, and `defaultSort()`
 * when `M` has a list action; see
 * route_muxws.
 */
export type MuxwsProxy<M> = ViewSetCalls<M> &
  ViewSetWatch<RowOf<M>, KeyOf<M>, FetchOption<RowOf<M>, RecordOf<M>, KeyOf<M>>> &
  DefaultSortOf<keyof M>;

type ViewSetClass = abstract new (...args: any[]) => any;

/**
 * The shape a factory-built class's constructor has, purely to give `route_muxws` an overload that
 * rejects it - see rest-proxy.ts's `FactoryBuiltClass` for why this has to be a plain overload
 * parameter rather than a type parameter conditioned on the argument.
 */
type FactoryBuiltClass = (abstract new (...args: any[]) => any) & {
  declares: { readonly [FACTORY_BUILT]: any };
};

/** Never assigned; its only use is `typeof FACTORY_BUILT_REJECTION` as a self-describing return type. */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- type-only, read via `typeof` below
declare const FACTORY_BUILT_REJECTION: 'route_muxws cannot take a factory-built class - extend it directly instead';

export interface MuxwsProxyOptions extends ProxyBaseOptions {
  peer: MuxwsPeerSource;
  /**
   * Per-attempt timeout in milliseconds, the wait for the reply headers included. muxws resets the
   * stream with TIMEOUT when it expires, and the call is repeated at once (see
   * `ProxyBaseOptions.repeat`). Unset, an attempt has no timeout.
   */
  timeoutMs?: number;
  /**
   * Headers added to every request this proxy makes. The WebSocket handshake already carries
   * whatever identified the session and the server treats that as the baseline; these override it
   * for this proxy's calls; `RequestOptions.headers` override these for a single call.
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

  /**
   * The peer: the source's result, kept until a function source's peer has closed for good; the
   * source is called again then, and after it threw or rejected. A bare peer is never replaced.
   */
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
   * Opens a stream for the call, with its `Idempotency-Key`, and repeats the call with the same key
   * while an attempt ends with ConnectionLost, StreamTimeout or StreamRefused, or `open()` throws
   * ConnectionClosed or ConnectionGoingAway while the peer has not closed for good. A repeat after a
   * lost connection waits for the peer to reconnect; a repeated attempt's stream is cancelled. A
   * repeat is opened with the headers of the call's first stream.
   *
   * The first attempt's stream is opened synchronously once the peer has resolved, so whatever the
   * caller binds for the synchronous part of the call (a muxws open interceptor reading it) reaches
   * that stream. Before the peer has resolved, and on a repeat, the stream is opened later.
   */
  protected request<R>(method: HttpMethod, path: string, options: RequestOptions = {}): ViewSetCall<R> {
    const key = options.key ?? newCallKey();
    let call: Promise<R>;
    try {
      // Addressing mirrors HTTP/2: :-prefixed pseudo-headers for the request line, plain keys for
      // real HTTP headers. muxws itself never reads any of them (SPEC WSM-AUT-002).
      const headers: Record<string, unknown> = {
        ...withKeyHeader({ ...this.headers, ...options.headers }, key),
        ':method': method,
        ':path': `${this.basePath}${path}`,
      };
      if (options.query && Object.keys(options.query).length > 0) headers[':query'] = cleanQuery(options.query);

      // Every attempt is opened with the headers the first stream went out with, those that open
      // interceptors added included: an interceptor that reads state bound only for the caller's
      // synchronous part does not see it when a repeat is opened.
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
    // end: true — this is a unary call, so the request is complete with its opening frame.
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
    // The status is in the answering side's leading headers. `result()` is started first so that
    // `timeoutMs` bounds the whole attempt, the wait for those headers included: a stream that never
    // answers rejects with muxws' StreamTimeout instead of waiting for headers that do not come.
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
 * The muxws errors after which no answer reached the client, by their `name` (muxws sets `name` on
 * every error class, and the package is not imported here). StreamTimeout is the attempt's own
 * timeout. ConnectionClosed is what `open()` throws while the peer is between sockets, and
 * ConnectionGoingAway while its connection drains after a goaway; a peer closed for good throws
 * either, and that is not repeated.
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

// Exists only to reject a factory-built class with a message at the call site; nothing implements
// or calls it - see rest-proxy.ts's route_rest for why.
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- M keeps the call syntax identical to the real overload
function route_muxws<M = never>(
  viewSetClass: FactoryBuiltClass,
  options: MuxwsProxyOptions,
): typeof FACTORY_BUILT_REJECTION;
/**
 * Registers a muxws proxy for the given ViewSet class. The mirror of route_rest, and it takes the
 * same generic parameter for the same reason: TypeScript cannot inspect the Python class.
 */
function route_muxws<M>(viewSetClass: ViewSetClass, options: MuxwsProxyOptions): MuxwsProxy<M>;
function route_muxws<M>(
  viewSetClass: ViewSetClass,
  options: MuxwsProxyOptions,
): MuxwsProxy<M> | typeof FACTORY_BUILT_REJECTION {
  // The class is otherwise used only for its type. Its `declares` is the one thing on it the proxy
  // needs at runtime, so it is carried across rather than lost.
  return new MuxwsProxyImpl({
    declares: (viewSetClass as { declares?: ViewSetMixinDeclaration[] }).declares,
    ...options,
  }) as unknown as MuxwsProxy<M>;
}

export { route_muxws };
