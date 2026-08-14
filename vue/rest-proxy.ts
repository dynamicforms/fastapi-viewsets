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
 * Everything except the sending lives in ViewSetProxyBase, which MuxwsProxyImpl shares — see
 * proxy-base.ts.
 */

import axios, { type AxiosInstance } from 'axios';

import type { KeyType } from './mixins';
import {
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
 * The REST proxy type is simply the mixin interface `M` the caller declares.
 * Because TypeScript cannot inspect Python class hierarchies at runtime, the
 * caller provides the explicit type via the generic parameter `M` (see route_rest).
 */
export type RestProxy<M> = M;

export interface RestProxyOptions extends ProxyBaseOptions {
  /** Optional: existing axios instance. Defaults to the global axios. */
  axiosInstance?: AxiosInstance;
}

// ---------------------------------------------------------------------------
// Proxy implementation
// ---------------------------------------------------------------------------

export class RestProxyImpl<K extends KeyType, T, PK extends keyof T> extends ViewSetProxyBase<K, T, PK> {
  protected readonly http: AxiosInstance;

  constructor(options: RestProxyOptions) {
    super(options);
    this.http = options.axiosInstance ?? axios;
    this.initSchemaValidation();
  }

  /**
   * Dispatches to axios' per-verb methods rather than to `http.request()`, deliberately. Those
   * are the calls this proxy has always made, and they are what application interceptors and test
   * doubles are written against — routing everything through `request()` would be invisible on
   * the wire but would break every one of them.
   *
   * axios throws its own AxiosError on a non-2xx, which already carries `response.status` — the
   * shape ViewSetRequestError mirrors for the muxws side. Nothing to translate here.
   */
  protected async request<R>(method: HttpMethod, path: string, options: RequestOptions = {}): Promise<R> {
    const url = `${this.basePath}${path}`;
    const hasQuery = options.query !== undefined && Object.keys(options.query).length > 0;
    const config = hasQuery ? { params: options.query } : undefined;

    switch (method) {
      case 'GET': {
        const res = config ? await this.http.get<R>(url, config) : await this.http.get<R>(url);
        return res.data;
      }
      // The config argument is omitted rather than passed as undefined: axios treats the two
      // identically, but a spy does not, and the call shape is part of what this proxy promises.
      case 'POST': {
        const res = config
          ? await this.http.post<R>(url, options.body, config)
          : await this.http.post<R>(url, options.body);
        return res.data;
      }
      case 'PUT': {
        const res = config
          ? await this.http.put<R>(url, options.body, config)
          : await this.http.put<R>(url, options.body);
        return res.data;
      }
      case 'PATCH': {
        const res = config
          ? await this.http.patch<R>(url, options.body, config)
          : await this.http.patch<R>(url, options.body);
        return res.data;
      }
      case 'DELETE': {
        // A DELETE body has to go through the config object; axios has no positional slot for it.
        const deleteConfig = options.body === undefined ? config : { ...(config ?? {}), data: options.body };
        const res = deleteConfig ? await this.http.delete<R>(url, deleteConfig) : await this.http.delete<R>(url);
        return res.data;
      }
    }
  }
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
function route_rest<M>(
  _viewSetClass: ViewSetClass,
  basePath: string,
  pkFieldName: string,
  axiosInstance?: AxiosInstance,
): RestProxy<M>;
function route_rest<M>(_viewSetClass: ViewSetClass, options: RestProxyOptions): RestProxy<M>;
function route_rest<M>(
  viewSetClass: ViewSetClass,
  basePathOrOptions: string | RestProxyOptions,
  pkFieldName?: string,
  axiosInstance?: AxiosInstance,
): RestProxy<M> {
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
