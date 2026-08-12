/**
 * Transport-independent half of the ViewSet proxies.
 *
 * Every ViewSet method is the same regardless of how the request travels — `list()` is always a
 * GET on the base path, `destroy(pk)` is always a DELETE on `/{pk}`. Only the sending differs, so
 * that is the only thing subclasses supply: one `request()` method. `RestProxyImpl` sends over
 * HTTP with axios, `MuxwsProxyImpl` sends over a muxws stream.
 *
 * Custom endpoints should be written against `request()` rather than against a transport, so that
 * the same ViewSet class works on either:
 *
 *   class MusicTrackViewSet extends RestProxyImpl<number, MusicTrack, 'id'> {
 *     async count(): Promise<number> {
 *       return this.request<number>('GET', '/count');
 *     }
 *   }
 */

import type {
  BulkViewSetMixin,
  DestroyReturnData,
  KeyType,
  ListParams,
  LookupItem,
  LookupMixin,
  PageParams,
  PaginatedList,
} from './mixins';

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

/** Query values; an array becomes a repeated key, which is how FastAPI binds `list[str]`. */
export type QueryParams = Record<string, string | number | boolean | null | undefined | Array<string | number>>;

export interface RequestOptions {
  query?: QueryParams;
  body?: unknown;
}

/**
 * What a failed call throws, on both transports.
 *
 * The shape deliberately mirrors `AxiosError` — `error.response.status` and
 * `error.response.data` — so that error handling written against the REST proxy keeps working
 * unchanged when the same code is pointed at muxws. Over HTTP axios throws its own error and this
 * class is not used; over muxws there is no axios, so it is.
 */
export class ViewSetRequestError extends Error {
  readonly response: { status: number; data: unknown; headers: Record<string, string> };

  constructor(status: number, data: unknown, headers: Record<string, string> = {}) {
    super(`Request failed with status code ${status}`);
    this.name = 'ViewSetRequestError';
    this.response = { status, data, headers };
  }
}

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

export interface ProxyBaseOptions {
  /** Base path to the resource, e.g. '/items'. */
  basePath: string;
  /** Name of the PK field on the model, e.g. 'id'. */
  pkFieldName: string;
  /** Set false to skip the startup schema check (it is advisory and costs one request). */
  validateSchema?: boolean;
}

export abstract class ViewSetProxyBase<K extends KeyType, T, PK extends keyof T>
  implements BulkViewSetMixin<K, T, PK>, LookupMixin
{
  protected readonly basePath: string;

  protected readonly pkFieldName: string;

  private readonly schemaValidationEnabled: boolean;

  protected constructor(options: ProxyBaseOptions) {
    this.basePath = options.basePath.replace(/\/$/, '');
    this.pkFieldName = options.pkFieldName;
    this.schemaValidationEnabled = options.validateSchema !== false;
  }

  /**
   * Starts the advisory schema check. Subclasses must call this as the *last* statement of their
   * constructor, never the base constructor itself: `request()` reads fields the subclass has not
   * assigned yet while `super()` is still running, and since the check swallows its own errors,
   * doing it here would leave it permanently and silently dead.
   */
  protected initSchemaValidation(): void {
    if (this.schemaValidationEnabled) void this.validateAgainstSchema();
  }

  /**
   * Sends one request and returns the decoded response body.
   *
   * `path` is relative to `basePath` — '' for the collection, '/1' for a record, '/bulk', and so
   * on. Implementations must throw on a non-2xx status, with `response.status` readable on the
   * thrown value.
   */
  protected abstract request<R>(method: HttpMethod, path: string, options?: RequestOptions): Promise<R>;

  /**
   * Fetches the BE schema and compares it against the FE method set.
   * Logs a console warning for any mismatch found.
   *
   * Non-critical: errors during fetch or parsing are silently ignored. Note that the schema is
   * fetched over this proxy's own transport, so a muxws proxy validates against the muxws
   * endpoint set and a REST proxy against the REST one — which is the point, since the two are
   * allowed to differ.
   */
  private async validateAgainstSchema(): Promise<void> {
    try {
      const schema = await this.request<{ paths?: Record<string, Record<string, unknown>> }>('GET', '/schema');
      const paths = schema?.paths ?? {};

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
    return this.request<T>('POST', '', { body: data });
  }

  async bulkCreate(data: Omit<T, PK>[]): Promise<T[]> {
    return this.request<T[]>('POST', '/bulk', { body: data });
  }

  async list(params?: ListParams): Promise<T[]> {
    return this.request<T[]>('GET', '', { query: params });
  }

  /**
   * Fetches one page. Only meaningful against a viewset built on the BE PaginatedListMixin — a
   * plain ListMixin ignores offset/limit and answers with the whole collection, which would not
   * match this return type.
   *
   * The BE speaks snake_case (`has_more`); the rest of this client speaks whatever the model
   * declares, so only the envelope's own fields are renamed here. The records inside are passed
   * through untouched.
   */
  async listPage(params?: PageParams): Promise<PaginatedList<T>> {
    const page = await this.request<{
      results: T[];
      offset: number;
      limit: number | null;
      count: number | null;
      has_more: boolean;
      has_previous: boolean;
    }>('GET', '', { query: params });
    return {
      results: page.results,
      offset: page.offset,
      limit: page.limit,
      count: page.count,
      hasMore: page.has_more,
      hasPrevious: page.has_previous,
    };
  }

  async retrieve(pk: K): Promise<T> {
    return this.request<T>('GET', `/${pk}`);
  }

  async update(pk: K, data: T): Promise<T> {
    return this.request<T>('PUT', `/${pk}`, { body: data });
  }

  async partialUpdate(pk: K, data: Partial<T>): Promise<T> {
    return this.request<T>('PATCH', `/${pk}`, { body: data });
  }

  async bulkUpdate(records: Record<K, T>): Promise<T[]> {
    return this.request<T[]>('PUT', '/bulk', { body: records });
  }

  async bulkPartialUpdate(records: Record<K, Partial<T>>): Promise<T[]> {
    return this.request<T[]>('PATCH', '/bulk', { body: records });
  }

  async destroy(pk: K): Promise<DestroyReturnData> {
    return this.request<DestroyReturnData>('DELETE', `/${pk}`);
  }

  async bulkDestroy(pks: K[]): Promise<DestroyReturnData[]> {
    return this.request<DestroyReturnData[]>('DELETE', '/bulk', { body: pks });
  }

  async lookup(): Promise<LookupItem[]> {
    return this.request<LookupItem[]>('GET', '/lookup');
  }
}
