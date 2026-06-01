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
// Schema validation constants
// ---------------------------------------------------------------------------

const HTTP_METHODS = new Set(['get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'trace']);

/**
 * Maps (path type, HTTP method) → FE method name for standard ViewSet endpoints.
 * Path types: 'base' = root, 'pk' = /{pk}, 'bulk' = /bulk, 'lookup' = /lookup.
 */
const ENDPOINT_TO_FE_METHOD: Readonly<Record<string, Readonly<Record<string, string>>>> = {
  base: { GET: 'list', POST: 'create' },
  pk: {
    GET: 'retrieve',
    PUT: 'update',
    PATCH: 'partialUpdate',
    DELETE: 'destroy',
  },
  bulk: {
    POST: 'bulkCreate',
    PUT: 'bulkUpdate',
    PATCH: 'bulkPartialUpdate',
    DELETE: 'bulkDestroy',
  },
  lookup: { GET: 'lookup' },
};

/** All standard FE method names, in a stable order for warning output. */
const STANDARD_FE_METHODS: readonly string[] = [
  'list',
  'create',
  'retrieve',
  'update',
  'partialUpdate',
  'destroy',
  'bulkCreate',
  'bulkUpdate',
  'bulkPartialUpdate',
  'bulkDestroy',
  'lookup',
];

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
    void this.validateAgainstSchema();
  }

  /**
   * Fetches the BE schema and compares it against the FE method set.
   * Logs a console warning for any mismatch found.
   * Non-critical: errors during fetch or parsing are silently ignored.
   */
  private async validateAgainstSchema(): Promise<void> {
    try {
      const res = await this.http.get<{
        paths?: Record<string, Record<string, unknown>>;
      }>(`${this.basePath}/schema`);
      const paths = res.data?.paths ?? {};

      const beMethods = new Set<string>();
      const unknownBeEndpoints: string[] = [];

      for (const [path, pathItem] of Object.entries(paths)) {
        const suffix = path.slice(this.basePath.length).replace(/^\//, '');

        let pathType: string;
        if (suffix === '') {
          pathType = 'base';
        } else if (suffix === 'bulk') {
          pathType = 'bulk';
        } else if (suffix === 'lookup') {
          pathType = 'lookup';
        } else if (suffix === 'schema') {
          continue;
        } else if (suffix.startsWith('{')) {
          pathType = 'pk';
        } else {
          for (const httpMethod of Object.keys(pathItem)) {
            if (HTTP_METHODS.has(httpMethod.toLowerCase())) {
              unknownBeEndpoints.push(`${httpMethod.toUpperCase()} ${path}`);
            }
          }
          continue;
        }

        const methodMap = ENDPOINT_TO_FE_METHOD[pathType] ?? {};
        for (const httpMethod of Object.keys(pathItem)) {
          if (!HTTP_METHODS.has(httpMethod.toLowerCase())) continue;
          const feMethod = methodMap[httpMethod.toUpperCase()];
          if (feMethod) beMethods.add(feMethod);
        }
      }

      const warnings: string[] = [];

      for (const method of STANDARD_FE_METHODS) {
        if (typeof (this as unknown as Record<string, unknown>)[method] === 'function' && !beMethods.has(method)) {
          warnings.push(`FE declares '${method}()' but BE has no matching endpoint`);
        }
      }

      for (const method of beMethods) {
        if (typeof (this as unknown as Record<string, unknown>)[method] !== 'function') {
          warnings.push(`BE exposes '${method}' endpoint but FE does not implement it`);
        }
      }

      for (const endpoint of unknownBeEndpoints) {
        warnings.push(`BE has non-standard endpoint '${endpoint}' with no FE method`);
      }

      if (warnings.length > 0) {
        console.warn(
          `[ViewSet ${this.basePath}] FE/BE definition mismatch:\n` + warnings.map((w) => `  • ${w}`).join('\n'),
        );
      }
    } catch {
      // Schema validation is non-critical; ignore fetch/parse errors silently
    }
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
      ? {
          basePath: basePathOrOptions,
          pkFieldName: pkFieldName!,
          axiosInstance,
        }
      : basePathOrOptions;
  return new RestProxyImpl(options) as unknown as RestProxy<M>;
}

export { route_rest };
