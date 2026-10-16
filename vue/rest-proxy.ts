/**
 * REST proxy for ViewSets — FE counterpart of the BE route_viewset decorator.
 *
 * Usage:
 *   const restItems = route_rest<BulkViewSetMixin<number, Item> & LookupMixin>(
 *     ItemViewSet, '/items', 'id',
 *   );
 *   const items = await restItems.list();
 *   const item  = await restItems.retrieve(1);
 *
 * Every call carries an `Idempotency-Key` and is repeated with it after a network error, an attempt
 * timeout or a gateway status; see call.ts.
 *
 * Everything except the sending lives in ViewSetProxyBase, which MuxwsProxyImpl shares — see
 * proxy-base.ts.
 */

import axios, { type AxiosInstance, type AxiosRequestConfig, type AxiosResponse } from 'axios';

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
import type { KeyType } from './mixins';
import {
  FACTORY_BUILT,
  type HttpMethod,
  type ProxyBaseOptions,
  type RequestOptions,
  type ViewSetMixinDeclaration,
  ViewSetProxyBase,
} from './proxy-base';

// ---------------------------------------------------------------------------
// Helper types
// ---------------------------------------------------------------------------

/** ViewSet class constructor (for type-level introspection only). */

type ViewSetClass = abstract new (...args: any[]) => any;

/**
 * The shape a factory-built class's constructor has, purely to give `route_rest` an overload that
 * rejects it - a type parameter conditioned on the argument (`C extends FactoryBuiltClass ? ... :
 * C`) cannot be inferred from that position at all, so an overload with this as a plain parameter
 * type is the only form that actually sees the real argument.
 *
 * `route_rest` uses its `viewSetClass` argument only for the `declares` on it (see the function
 * body - the class itself is never `new`'d), then builds a bare `RestProxyImpl` and hands it back
 * cast to `M`. Pass a factory-built class and this still type-checks without the overload below -
 * `M` is usually inferred as `InstanceType<typeof ItemApi>` - but the object it returns is not an
 * `ItemApi`, so any custom method the factory-built class added is `undefined` at runtime despite
 * compiling. `declares` is required here, not optional: a hand-written class
 * (`class ItemViewSet extends RestProxyImpl<...> {}`) may have no `declares` at all, or one that is
 * a plain array rather than carrying `FACTORY_BUILT`, and either must fall through to the real
 * overload below rather than match this one.
 */
type FactoryBuiltClass = (abstract new (...args: any[]) => any) & {
  declares: { readonly [FACTORY_BUILT]: any };
};

/** Never assigned; its only use is `typeof FACTORY_BUILT_REJECTION` as a self-describing return type. */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- type-only, read via `typeof` below
declare const FACTORY_BUILT_REJECTION: 'route_rest cannot take a factory-built class - extend it directly instead';

/**
 * The REST proxy type is the mixin interface `M` the caller declares, with every method returning a
 * `ViewSetCall`, and `watch()`. Because TypeScript cannot inspect Python class hierarchies at runtime, the caller
 * provides the explicit type via the generic parameter `M` (see route_rest).
 */
export type RestProxy<M> = ViewSetCalls<M> & ViewSetWatch<RecordOf<M>, KeyOf<M>>;

export interface RestProxyOptions extends ProxyBaseOptions {
  /** Optional: existing axios instance. Defaults to the global axios. */
  axiosInstance?: AxiosInstance;
  /**
   * The timeout of one attempt, in milliseconds, passed to axios as `timeout`. An attempt that
   * reaches it is repeated at once (see `ProxyBaseOptions.repeat`). `0` means no timeout. Default
   * 30000.
   */
  attemptTimeoutMs?: number;
}

// ---------------------------------------------------------------------------
// Proxy implementation
// ---------------------------------------------------------------------------

export class RestProxyImpl<K extends KeyType, T, PK extends keyof T> extends ViewSetProxyBase<K, T, PK> {
  protected readonly http: AxiosInstance;

  private readonly attemptTimeoutMs: number;

  constructor(options: RestProxyOptions) {
    super(options);
    this.http = options.axiosInstance ?? axios;
    this.attemptTimeoutMs = options.attemptTimeoutMs ?? DEFAULT_ATTEMPT_TIMEOUT_MS;
    this.initSchemaValidation();
  }

  /**
   * Sends the call with its `Idempotency-Key` and repeats it with the same key while an attempt ends
   * without an answer: an axios error without `response`, or a 502, 503 or 504 from a gateway (see
   * `restAttemptFailure`). A repeated attempt is aborted through its `AbortSignal`.
   *
   * axios throws its own AxiosError on a non-2xx, which already carries `response.status` — the
   * shape ViewSetRequestError mirrors for the muxws side. Nothing to translate here.
   */
  protected request<R>(method: HttpMethod, path: string, options: RequestOptions = {}): ViewSetCall<R> {
    const key = options.key ?? newCallKey();
    let call: Promise<R>;
    try {
      const url = `${this.basePath}${path}`;
      const headers = withKeyHeader({ ...options.headers }, key);
      // Every attempt is sent with the headers the first one went out with, those that request
      // interceptors added included: an interceptor that reads state bound only for the caller's
      // synchronous part does not see it when a repeat is sent.
      let sent: Record<string, string> | undefined;
      call = runCall(
        () =>
          this.attempt<R>(method, url, options, sent ?? headers, (config) => (sent ??= sentHeaders(config, headers))),
        restAttemptFailure,
        options.repeat ?? this.repeatOptions,
      );
    } catch (error) {
      call = Promise.reject(error);
    }
    return issueCall(call, key);
  }

  /**
   * One attempt. Dispatches to axios' per-verb methods rather than to `http.request()`: those are what
   * application interceptors and test doubles are written against — routing everything through
   * `request()` would be invisible on the wire but would break every one of them.
   */
  private attempt<R>(
    method: HttpMethod,
    url: string,
    options: RequestOptions,
    headers: Record<string, string>,
    onFailure: (config: AxiosRequestConfig | undefined) => void,
  ): CallAttempt<R> {
    const controller = new AbortController();
    const config: AxiosRequestConfig = {
      ...(options.query !== undefined && Object.keys(options.query).length > 0 ? { params: options.query } : {}),
      headers,
      timeout: this.attemptTimeoutMs,
      signal: controller.signal,
    };

    let response: Promise<AxiosResponse<R>>;
    switch (method) {
      case 'GET':
        response = this.http.get<R>(url, config);
        break;
      case 'POST':
        response = this.http.post<R>(url, options.body, config);
        break;
      case 'PUT':
        response = this.http.put<R>(url, options.body, config);
        break;
      case 'PATCH':
        response = this.http.patch<R>(url, options.body, config);
        break;
      case 'DELETE':
        // A DELETE body has to go through the config object; axios has no positional slot for it.
        response = this.http.delete<R>(url, options.body === undefined ? config : { ...config, data: options.body });
        break;
    }
    return {
      result: Promise.resolve(response).then(
        (res) => res.data,
        (error: unknown) => {
          onFailure(axios.isAxiosError(error) ? error.config : undefined);
          throw error;
        },
      ),
      abandon: () => controller.abort(),
    };
  }
}

/**
 * Headers axios sets from the request itself, which a repeat leaves to axios to set again: a body
 * type it derives (a `FormData` boundary) must not be carried over.
 */
const TRANSPORT_HEADERS = new Set(['accept', 'accept-encoding', 'content-length', 'content-type', 'user-agent']);

/**
 * The headers an attempt was sent with, after the request interceptors, as its `config` holds them:
 * `headers` over every string header of `config` other than `TRANSPORT_HEADERS`. `headers` without a
 * `config`.
 */
function sentHeaders(config: AxiosRequestConfig | undefined, headers: Record<string, string>): Record<string, string> {
  const raw = config?.headers as { toJSON?: () => Record<string, unknown> } | Record<string, unknown> | undefined;
  if (!raw) return headers;
  const flat = (typeof raw.toJSON === 'function' ? raw.toJSON() : raw) as Record<string, unknown>;
  const own = new Set(Object.keys(headers).map((name) => name.toLowerCase()));
  const out: Record<string, string> = {};
  for (const [name, value] of Object.entries(flat)) {
    const lower = name.toLowerCase();
    if (typeof value === 'string' && !TRANSPORT_HEADERS.has(lower) && !own.has(lower)) out[name] = value;
  }
  return { ...out, ...headers };
}

/** The default of `RestProxyOptions.attemptTimeoutMs`. */
const DEFAULT_ATTEMPT_TIMEOUT_MS = 30_000;

/** The statuses a gateway answers when the application behind it did not. */
const GATEWAY_STATUSES = new Set([502, 503, 504]);

/**
 * An axios error without `response` is an attempt that got no answer: `'timeout'` for the attempt's
 * own timeout (`ECONNABORTED`, `ETIMEDOUT`), `'lost'` otherwise. A cancellation the application
 * requested is an answer. A gateway status is `'lost'` unless its body is a JSON object with `detail`,
 * the body FastAPI gives an `HTTPException`: that status is the application's answer (e.g. the BE's
 * `result_timeout`), which `Idempotency` stores and a repeat would only receive again.
 */
function restAttemptFailure(error: unknown): AttemptFailure {
  if (!axios.isAxiosError(error) || axios.isCancel(error)) return null;
  if (error.response === undefined) {
    return error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT' ? 'timeout' : 'lost';
  }
  if (!GATEWAY_STATUSES.has(error.response.status)) return null;
  const data: unknown = error.response.data;
  return typeof data === 'object' && data !== null && 'detail' in data ? null : 'lost';
}

// ---------------------------------------------------------------------------
// Decorator / factory
// ---------------------------------------------------------------------------

/**
 * Registers a REST proxy for the given ViewSet class.
 *
 * The generic parameter `M` determines which mixin interfaces are available —
 * typically the ViewSet type (or a union of mixin interfaces).
 *
 * @example
 * ```ts
 * import type { BulkViewSetMixin, LookupMixin } from './mixins';
 *
 * interface Item { id: number; name: string }
 *
 * // with separate arguments (recommended)
 * const restItems = route_rest<BulkViewSetMixin<number, Item> & LookupMixin>(
 *   ItemViewSet, '/items', 'id',
 * );
 *
 * // or with an options object
 * const restItems2 = route_rest<BulkViewSetMixin<number, Item> & LookupMixin>(
 *   ItemViewSet, { basePath: '/items', pkFieldName: 'id' },
 * );
 *
 * const items = await restItems.list();
 * const item  = await restItems.retrieve(1);
 * ```
 */
// These two overloads exist only to reject a factory-built class with a message at the call site;
// nothing implements or calls them - overload resolution tries them first, and a factory-built
// class's constructor matches FactoryBuiltClass before it ever reaches the real overloads below.
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- M keeps the call syntax identical to the real overload
function route_rest<M = never>(
  viewSetClass: FactoryBuiltClass,
  basePath: string,
  pkFieldName: string,
  axiosInstance?: AxiosInstance,
): typeof FACTORY_BUILT_REJECTION;
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- M keeps the call syntax identical to the real overload
function route_rest<M = never>(
  viewSetClass: FactoryBuiltClass,
  options: RestProxyOptions,
): typeof FACTORY_BUILT_REJECTION;
// The positional form takes no `events`, so its type carries no `watch()`.
function route_rest<M>(
  _viewSetClass: ViewSetClass,
  basePath: string,
  pkFieldName: string,
  axiosInstance?: AxiosInstance,
): ViewSetCalls<M>;
function route_rest<M>(_viewSetClass: ViewSetClass, options: RestProxyOptions): RestProxy<M>;
function route_rest<M>(
  viewSetClass: ViewSetClass,
  basePathOrOptions: string | RestProxyOptions,
  pkFieldName?: string,
  axiosInstance?: AxiosInstance,
): RestProxy<M> | ViewSetCalls<M> | typeof FACTORY_BUILT_REJECTION {
  const options: RestProxyOptions =
    typeof basePathOrOptions === 'string'
      ? {
          basePath: basePathOrOptions,
          pkFieldName: pkFieldName!,
          axiosInstance,
        }
      : basePathOrOptions;
  // The class is otherwise used only for its type. Its `declares` is the one thing on it the proxy
  // needs at runtime, so it is carried across rather than lost.
  return new RestProxyImpl({
    declares: (viewSetClass as { declares?: ViewSetMixinDeclaration[] }).declares,
    ...options,
  }) as unknown as RestProxy<M>;
}

export { route_rest };
