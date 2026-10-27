/**
 * REST proxy for ViewSets: the FE counterpart of the BE `route_viewset` decorator. Only the sending
 * lives here; the rest is in ViewSetProxyBase (proxy-base.ts). Every call carries an `Idempotency-Key`
 * and is repeated with it after a network error, an attempt timeout or a gateway status (call.ts).
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
import { isApiErrorBody } from './errors';
import type { KeyOf, RecordOf, ViewSetWatch } from './events/changes';
import type { DefaultSortOf, KeyType } from './mixins';
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
 * A factory-built class's constructor, matched by the `route_rest` overloads that reject it:
 * `route_rest` never instantiates the class, so the factory-built class's own methods would be
 * `undefined` on the proxy it returns. `declares` is required so that a hand-written class falls
 * through to the real overloads.
 */
type FactoryBuiltClass = (abstract new (...args: any[]) => any) & {
  declares: { readonly [FACTORY_BUILT]: any };
};

/** Never assigned; its only use is `typeof FACTORY_BUILT_REJECTION` as a self-describing return type. */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- type-only, read via `typeof` below
declare const FACTORY_BUILT_REJECTION: 'route_rest cannot take a factory-built class - extend it directly instead';

/**
 * The mixin interface `M` the caller declares, with every method returning a `ViewSetCall`, plus
 * `watch()`, and `defaultSort()` when `M` has a list action.
 */
export type RestProxy<M> = ViewSetCalls<M> & ViewSetWatch<RecordOf<M>, KeyOf<M>> & DefaultSortOf<keyof M>;

export interface RestProxyOptions extends ProxyBaseOptions {
  /** Optional: existing axios instance. Defaults to the global axios. */
  axiosInstance?: AxiosInstance;
  /**
   * The timeout of one attempt in milliseconds (axios `timeout`); an attempt that reaches it is
   * repeated at once. `0` means no timeout. Default 30000.
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
   * without an answer (see `restAttemptFailure`). A non-2xx rejects with axios' own AxiosError.
   */
  protected request<R>(method: HttpMethod, path: string, options: RequestOptions = {}): ViewSetCall<R> {
    const key = options.key ?? newCallKey();
    let call: Promise<R>;
    try {
      const url = `${this.basePath}${path}`;
      const headers = withKeyHeader({ ...options.headers }, key);
      // A repeat is sent with the first attempt's headers, interceptor-added ones included: an
      // interceptor reading state bound only for the caller's synchronous part does not see it later.
      let sent: Record<string, string> | undefined;
      call = runCall(
        () =>
          this.attempt<R>(method, url, options, sent ?? headers, (config) => (sent ??= sentHeaders(config, headers))),
        restAttemptFailure,
        options.repeat ?? this.repeatOptions,
        undefined,
        key,
      );
    } catch (error) {
      call = Promise.reject(error);
    }
    return issueCall(call, key, options.background, method);
  }

  /** One attempt, through axios' per-verb methods: application test doubles stub those, not `request()`. */
  private attempt<R>(
    method: HttpMethod,
    url: string,
    options: RequestOptions,
    headers: Record<string, string>,
    onFailure: (config: AxiosRequestConfig | undefined) => void,
  ): CallAttempt<R> {
    const controller = new AbortController();
    const config: AxiosRequestConfig = {
      // An array goes out as a repeated key (`ids=1&ids=2`), the form FastAPI reads into a list;
      // axios' default, `ids[]=1&ids[]=2`, reaches FastAPI as an unknown `ids[]` parameter.
      ...(options.query !== undefined && Object.keys(options.query).length > 0
        ? { params: options.query, paramsSerializer: { indexes: null } }
        : {}),
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

/** The headers an attempt went out with after the request interceptors, less `TRANSPORT_HEADERS`. */
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
 * An axios error without `response` got no answer: `'timeout'` for the attempt's own timeout, `'lost'`
 * otherwise. A gateway status (502-504) is `'lost'` unless its body is an `ApiErrorBody`: then it is
 * the application's answer, which a repeat would only receive again.
 */
function restAttemptFailure(error: unknown): AttemptFailure {
  if (!axios.isAxiosError(error) || axios.isCancel(error)) return null;
  if (error.response === undefined) {
    return error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT' ? 'timeout' : 'lost';
  }
  if (!GATEWAY_STATUSES.has(error.response.status)) return null;
  return isApiErrorBody(error.response.data) ? null : 'lost';
}

// ---------------------------------------------------------------------------
// Decorator / factory
// ---------------------------------------------------------------------------

/**
 * Builds a REST proxy for a ViewSet; `M` names the mixin interfaces it exposes.
 *
 * @example
 * const items = route_rest<BulkViewSetMixin<number, Item>>(ItemViewSet, '/items', 'id');
 * const item = await items.retrieve(1);
 */
// The first two overloads reject a factory-built class with a message at the call site.
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
  // The class is used only for its type and its `declares`.
  return new RestProxyImpl({
    declares: (viewSetClass as { declares?: ViewSetMixinDeclaration[] }).declares,
    ...options,
  }) as unknown as RestProxy<M>;
}

export { route_rest };
