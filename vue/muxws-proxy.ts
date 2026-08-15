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

import type { KeyType } from './mixins';
import {
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
}

export interface MuxwsPeerLike {
  open(payload?: unknown, options?: { headers?: Record<string, unknown>; end?: boolean }): MuxwsStreamLike;
}

/**
 * Where the peer comes from. A bare peer is the simple case; a function is for the common shape
 * where the proxy is constructed at module scope but `connect()` has not resolved yet. The
 * function is called once and its result cached — a muxws Peer survives its own reconnects, so
 * there is nothing to re-resolve afterwards.
 */
export type MuxwsPeerSource = MuxwsPeerLike | (() => MuxwsPeerLike | Promise<MuxwsPeerLike>);

export type MuxwsProxy<M> = M;

type ViewSetClass = abstract new (...args: any[]) => any;

export interface MuxwsProxyOptions extends ProxyBaseOptions {
  peer: MuxwsPeerSource;
  /** Per-request timeout in milliseconds. muxws resets the stream with TIMEOUT when it expires. */
  timeoutMs?: number;
  /**
   * Headers added to every request this proxy makes. The WebSocket handshake already carries
   * whatever identified the session and the server treats that as the baseline; these override it
   * for this proxy's calls. There is no per-call header: `RequestOptions` is `{ query, body }`.
   */
  headers?: Record<string, string>;
}

export class MuxwsProxyImpl<K extends KeyType, T, PK extends keyof T> extends ViewSetProxyBase<K, T, PK> {
  private readonly peerSource: MuxwsPeerSource;

  private readonly timeoutMs?: number;

  private readonly headers: Record<string, string>;

  private peerPromise?: Promise<MuxwsPeerLike>;

  constructor(options: MuxwsProxyOptions) {
    super(options);
    this.peerSource = options.peer;
    this.timeoutMs = options.timeoutMs;
    this.headers = options.headers ?? {};
    this.initSchemaValidation();
  }

  private peer(): Promise<MuxwsPeerLike> {
    if (!this.peerPromise) {
      const source = this.peerSource;
      this.peerPromise = Promise.resolve(typeof source === 'function' ? source() : source);
    }
    return this.peerPromise;
  }

  protected async request<R>(method: HttpMethod, path: string, options: RequestOptions = {}): Promise<R> {
    const peer = await this.peer();

    // Addressing mirrors HTTP/2: :-prefixed pseudo-headers for the request line, plain keys for
    // real HTTP headers. muxws itself never reads any of them (SPEC WSM-AUT-002).
    const headers: Record<string, unknown> = {
      ...this.headers,
      ':method': method,
      ':path': `${this.basePath}${path}`,
    };
    if (options.query && Object.keys(options.query).length > 0) headers[':query'] = cleanQuery(options.query);

    // end: true — this is a unary call, so the request is complete with its opening frame.
    const stream = peer.open(options.body, { headers, end: true });

    // The status is announced before the body, in the answering side's leading headers (muxws
    // 0.3.1+). Awaiting the gate first means an error status is known without reading the response
    // at all — which is what makes this work for a streaming reply and not only a unary one.
    await stream.replyHeadersArrived;
    const status = readStatus(stream.replyHeaders);

    const body = await stream.result(this.timeoutMs === undefined ? undefined : { timeoutMs: this.timeoutMs });
    if (status >= 400) throw new ViewSetRequestError(status, body, readHeaders(stream.replyHeaders));
    return body as R;
  }
}

function cleanQuery(query: QueryParams): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    out[key] = value;
  }
  return out;
}

/**
 * A response with no `:status` is treated as 200. That is what a server which answered normally
 * looks like on a transport that has not been taught to say otherwise, and inventing a failure
 * for it would break every such server.
 */
function readStatus(replyHeaders: Record<string, unknown> | null | undefined): number {
  const raw = replyHeaders?.[':status'];
  if (typeof raw === 'number') return raw;
  if (typeof raw === 'string') {
    const parsed = Number.parseInt(raw, 10);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return 200;
}

function readHeaders(replyHeaders: Record<string, unknown> | null | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(replyHeaders ?? {})) {
    if (!key.startsWith(':')) out[key] = String(value);
  }
  return out;
}

/**
 * Registers a muxws proxy for the given ViewSet class. The mirror of route_rest, and it takes the
 * same generic parameter for the same reason: TypeScript cannot inspect the Python class.
 */
function route_muxws<M>(viewSetClass: ViewSetClass, options: MuxwsProxyOptions): MuxwsProxy<M> {
  // The class is otherwise used only for its type. Its `declares` is the one thing on it the proxy
  // needs at runtime, so it is carried across rather than lost.
  return new MuxwsProxyImpl({
    declares: (viewSetClass as { declares?: ViewSetMixinDeclaration[] }).declares,
    ...options,
  }) as unknown as MuxwsProxy<M>;
}

export { route_muxws };
