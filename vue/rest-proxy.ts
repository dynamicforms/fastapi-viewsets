/**
 * REST proxy for ViewSets — FE counterpart of the BE route_viewset decorator.
 *
 * Usage:
 *   const restItems = route_rest<BulkViewSetMixin<number, Item> & LookupMixin>(
 *     ItemViewSet, '/items', 'id',
 *   );
 *   const items = await restItems.list();
 *   const item  = await restItems.retrieve(1);
 */

import axios, { type AxiosInstance } from 'axios';

import type { BulkViewSetMixin, DestroyReturnData, KeyType, LookupItem, LookupMixin } from './mixins';

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

export interface RestProxyOptions {
  /** Base path to the resource, e.g. '/items'. */
  basePath: string;
  /** Name of the PK field on the model, e.g. 'id'. */
  pkFieldName: string;
  /** Optional: existing axios instance. Defaults to the global axios. */
  axiosInstance?: AxiosInstance;
}

// ---------------------------------------------------------------------------
// Proxy implementation
// ---------------------------------------------------------------------------

export class RestProxyImpl<K extends KeyType, T, PK extends keyof T>
  implements BulkViewSetMixin<K, T, PK>, LookupMixin
{
  protected readonly http: AxiosInstance;

  protected readonly basePath: string;

  protected readonly pkFieldName: string;

  constructor(options: RestProxyOptions) {
    this.basePath = options.basePath.replace(/\/$/, '');
    this.pkFieldName = options.pkFieldName;
    this.http = options.axiosInstance ?? axios;
  }

  async create(data: Omit<T, PK>): Promise<T> {
    const res = await this.http.post<T>(this.basePath, data);
    return res.data;
  }

  async bulkCreate(data: Omit<T, PK>[]): Promise<T[]> {
    const res = await this.http.post<T[]>(`${this.basePath}/bulk`, data);
    return res.data;
  }

  async list(): Promise<T[]> {
    const res = await this.http.get<T[]>(this.basePath);
    return res.data;
  }

  async retrieve(pk: K): Promise<T> {
    const res = await this.http.get<T>(`${this.basePath}/${pk}`);
    return res.data;
  }

  async update(pk: K, data: T): Promise<T> {
    const res = await this.http.put<T>(`${this.basePath}/${pk}`, data);
    return res.data;
  }

  async partialUpdate(pk: K, data: Partial<T>): Promise<T> {
    const res = await this.http.patch<T>(`${this.basePath}/${pk}`, data);
    return res.data;
  }

  async bulkUpdate(records: Record<K, T>): Promise<T[]> {
    const res = await this.http.put<T[]>(`${this.basePath}/bulk`, records);
    return res.data;
  }

  async bulkPartialUpdate(records: Record<K, Partial<T>>): Promise<T[]> {
    const res = await this.http.patch<T[]>(`${this.basePath}/bulk`, records);
    return res.data;
  }

  async destroy(pk: K): Promise<DestroyReturnData> {
    const res = await this.http.delete<DestroyReturnData>(`${this.basePath}/${pk}`);
    return res.data;
  }

  async bulkDestroy(pks: K[]): Promise<DestroyReturnData[]> {
    const res = await this.http.delete<DestroyReturnData[]>(`${this.basePath}/bulk`, { data: pks });
    return res.data;
  }

  async lookup(): Promise<LookupItem[]> {
    const res = await this.http.get<LookupItem[]>(`${this.basePath}/lookup`);
    return res.data;
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
  _viewSetClass: ViewSetClass,
  basePathOrOptions: string | RestProxyOptions,
  pkFieldName?: string,
  axiosInstance?: AxiosInstance,
): RestProxy<M> {
  const options: RestProxyOptions =
    typeof basePathOrOptions === 'string'
      ? { basePath: basePathOrOptions, pkFieldName: pkFieldName!, axiosInstance }
      : basePathOrOptions;
  return new RestProxyImpl(options) as unknown as RestProxy<M>;
}

export { route_rest };
