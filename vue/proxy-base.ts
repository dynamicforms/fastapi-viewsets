/**
 * Transport-independent half of the ViewSet proxies: every ViewSet method maps to one `request()`,
 * which `RestProxyImpl` (axios) and `MuxwsProxyImpl` (muxws stream) implement. Custom endpoints
 * call `request()` so that one ViewSet class works on either transport.
 *
 * Every call carries an `Idempotency-Key` and is repeated with it when an attempt ends without an
 * answer; see call.ts.
 */

import { bindCallKeys, type RepeatOptions, type ViewSetActionCall, type ViewSetCall } from './call';
import { type ViewSetWatch, watchRecords, type WatchOptions } from './events/changes';
import type { EventClient, EventSubscription } from './events/client';
import type {
  ActionName,
  BulkViewSetMixin,
  CursorListMixin,
  CursorPage,
  CursorParams,
  DestroyReturnData,
  KeyType,
  ListParams,
  LookupItem,
  LookupParams,
  LookupMixin,
  PageParams,
  PaginatedList,
  PaginatedListMixin,
  ListDefaultSort,
} from './mixins';
import { parseSort, type SortState } from './sort';

export { ViewSetRequestError } from './errors';

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

/** Query values; an array becomes a repeated key, which is how FastAPI binds `list[str]`. */
export type QueryParams = Record<string, string | number | boolean | null | undefined | Array<string | number>>;

export interface RequestOptions {
  query?: QueryParams;
  body?: unknown;
  /** Headers for this call only; on muxws they override the proxy's own `headers`. */
  headers?: Record<string, string>;
  /**
   * The `Idempotency-Key` of this call; default a new UUID. Pass the first call's `key` to repeat it.
   * Overrides an `Idempotency-Key` in `headers`.
   */
  key?: string;
  /** Overrides the proxy's `repeat` for this call. */
  repeat?: RepeatOptions;
  /** Reported to `onCallActivity` listeners, so a busy indicator can leave the call out. Default false. */
  background?: boolean;
}

/** The response shapes of `GET {basePath}`, as the BE names them in `X-List-Shape`. */
export type ListShape = 'plain' | 'paginated' | 'cursor';

/**
 * Thrown by `list()`, `listPage()` and `listCursor()` when the BE answers in another shape, e.g. a
 * viewset declaring a single shape ignores `X-List-Shape`. `received` is `'unknown'` when the body
 * matches none of the three envelopes.
 */
export class ListShapeMismatchError extends Error {
  readonly expected: ListShape;
  readonly received: ListShape | 'unknown';

  constructor(expected: ListShape, received: ListShape | 'unknown') {
    super(`Expected a '${expected}' list from the BE, received '${received}'`);
    this.name = 'ListShapeMismatchError';
    this.expected = expected;
    this.received = received;
  }
}

/** Identifies a list response by its envelope: an array, an object with `offset`, or one with `next` and `has_more`. */
function listShapeOf(body: unknown): ListShape | 'unknown' {
  if (Array.isArray(body)) return 'plain';
  if (typeof body !== 'object' || body === null || !Array.isArray((body as { results?: unknown }).results)) {
    return 'unknown';
  }
  if ('offset' in body) return 'paginated';
  if ('next' in body && 'has_more' in body) return 'cursor';
  return 'unknown';
}

// ---------------------------------------------------------------------------
// Schema validation constants
// ---------------------------------------------------------------------------

const HTTP_METHODS = new Set(['get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'trace']);

/** The OpenAPI extension in which the BE names the method serving an operation, e.g. `top_rated`. */
const ACTION_EXTENSION = 'x-viewset-action';

/** The FE method names that serve the BE method `action`: `top_rated` → `topRated()` or `top_rated()`. */
function customMethodNames(action: string): string[] {
  const camel = action.replace(/(?<=[a-zA-Z0-9])_+([a-z0-9])/g, (_, char: string) => char.toUpperCase());
  return camel === action ? [action] : [camel, action];
}

/**
 * Maps (path type, HTTP method) → the alternative actions that endpoint satisfies. Path types:
 * 'base' = root, 'pk' = /{pk}, 'bulk' = /bulk, 'lookup' = /lookup. `GET {base}` serves all three
 * list shapes (see list_shapes.py), so any one of them declared counts as a match.
 */
const ENDPOINT_TO_FE_METHOD: Readonly<Record<string, Readonly<Record<string, readonly ActionName[]>>>> = {
  base: { GET: ['list', 'listPage', 'listCursor'], POST: ['create'] },
  pk: {
    GET: ['retrieve'],
    PUT: ['update'],
    PATCH: ['partialUpdate'],
    DELETE: ['destroy'],
  },
  bulk: {
    POST: ['bulkCreate'],
    PUT: ['bulkUpdate'],
    PATCH: ['bulkPartialUpdate'],
    DELETE: ['bulkDestroy'],
  },
  lookup: { GET: ['lookup'] },
};

/**
 * Every `ActionName`, in a stable order for warning output. A `Record` so that a change to the
 * `ActionName` union without a matching change here fails to compile.
 */
const ACTION_NAME_COVERAGE: Record<ActionName, true> = {
  list: true,
  listPage: true,
  listCursor: true,
  create: true,
  retrieve: true,
  update: true,
  partialUpdate: true,
  destroy: true,
  bulkCreate: true,
  bulkUpdate: true,
  bulkPartialUpdate: true,
  bulkDestroy: true,
  lookup: true,
};

/** All standard FE method names, in a stable order for warning output. */
const STANDARD_FE_METHODS: readonly ActionName[] = Object.keys(ACTION_NAME_COVERAGE) as ActionName[];

/** The parts of the BE viewset's OpenAPI document this module reads. */
interface SchemaDocument {
  paths?: Record<string, Record<string, unknown>>;
}

/**
 * The viewset's base path as the schema document states it, taken from its /schema path. Document
 * paths lack the application's router prefixes, so they need not share a prefix with `basePath`,
 * which is the fallback for a document without /schema.
 */
function schemaBase(paths: Record<string, unknown>, basePath: string): string {
  return (
    Object.keys(paths)
      .filter((path) => path.endsWith('/schema'))
      .sort((a, b) => a.length - b.length)[0]
      ?.slice(0, -'/schema'.length) ?? basePath
  );
}

/** One entry of a ViewSet's `static declares` list: a mixin naming the actions it contributes. */
export interface ViewSetMixinDeclaration {
  readonly actions: readonly string[];
}

/**
 * Marks a class's `declares` as coming from the ViewSet factory (viewset.ts's `FactoryDeclares<D>`).
 * `route_rest` / `route_muxws` use it to reject a factory-built class, for which they would return a
 * bare proxy without the class's custom methods.
 */
export declare const FACTORY_BUILT: unique symbol;

/**
 * Which actions this ViewSet claims to have, from its `static declares` (a subclass's own list
 * replaces its parent's). Null when nothing is declared, as opposed to an empty declared list.
 */
function declaredActions(instance: object, fromOptions?: readonly ViewSetMixinDeclaration[]): Set<string> | null {
  const declares = fromOptions ?? (instance.constructor as { declares?: readonly ViewSetMixinDeclaration[] }).declares;
  if (!Array.isArray(declares)) return null;

  const actions = new Set<string>();
  for (const mixin of declares) for (const action of mixin?.actions ?? []) actions.add(action);
  return actions;
}

export interface ProxyBaseOptions {
  /** Base path to the resource, e.g. '/items'. */
  basePath: string;
  /** Name of the PK field on the model, e.g. 'id'. */
  pkFieldName: string;
  /** Set false to skip the startup schema check (it is advisory and costs one request). */
  validateSchema?: boolean;
  /** The mixins the ViewSet declares, for `route_rest` / `route_muxws`, whose bare proxy has no `static declares`. */
  declares?: readonly ViewSetMixinDeclaration[];
  /** When the proxy repeats a call whose attempt ended without an answer. See `RepeatOptions`. */
  repeat?: RepeatOptions;
  /**
   * The event client `watch()` subscribes on. A muxws ViewSet without it uses the client registered
   * for its peer; a REST ViewSet without it throws from `watch()`.
   */
  events?: EventClient;
  /**
   * The client topic `watch()` subscribes: the BE path relative to the app's `root_path`, without a
   * Mount prefix. Default `basePath`.
   */
  topic?: string;
}

/**
 * What a ViewSet's own methods may reach: everything a custom endpoint needs, nothing a caller does.
 * A class rather than a type because `protected` is checked nominally; the ViewSet factory types its
 * classes as this plus the declared actions.
 */
export abstract class ViewSetInternals {
  protected readonly basePath: string;

  protected constructor(basePath: string) {
    this.basePath = basePath.replace(/\/$/, '');
  }

  /**
   * Sends one call and returns the decoded body on a `ViewSetCall` carrying its `Idempotency-Key`.
   * The first attempt is issued synchronously. `path` is relative to `basePath` ('', '/1', '/bulk').
   * Throws on status >= 400 with `response.status` on the error; a 3xx is returned by muxws and
   * followed by REST.
   *
   * Concrete but never reached: abstract here would fail TS2515 on every factory-built ViewSet;
   * ViewSetProxyBase re-declares it abstract.
   */
  // eslint-disable-next-line @typescript-eslint/no-unused-vars -- the signature is the point; the body cannot run
  protected request<R>(method: HttpMethod, path: string, options?: RequestOptions): ViewSetCall<R> {
    throw new Error('ViewSetInternals.request: no transport');
  }
}

export abstract class ViewSetProxyBase<K extends KeyType, T, PK extends keyof T>
  extends ViewSetInternals
  implements
    BulkViewSetMixin<K, T, PK>,
    CursorListMixin<T>,
    PaginatedListMixin<T>,
    LookupMixin,
    ListDefaultSort,
    ViewSetWatch<T, K>
{
  /**
   * The mixins this ViewSet is composed of, e.g. `[ReadOnlyViewSetMixin, LookupMixin]`. Left undefined,
   * the ViewSet is not checked against the BE schema.
   */
  static declares?: readonly ViewSetMixinDeclaration[];

  /** Which field of `T` is the primary key, e.g. `'id'`; public for generic callers such as a grid. */
  readonly pkFieldName: string;

  private readonly schemaValidationEnabled: boolean;

  private readonly declaredMixins?: readonly ViewSetMixinDeclaration[];

  /** `ProxyBaseOptions.repeat`, for the transport's `request()`. */
  protected readonly repeatOptions?: RepeatOptions;

  /** `ProxyBaseOptions.events`. */
  protected readonly eventClientOption?: EventClient;

  /** `ProxyBaseOptions.topic`. */
  protected readonly topicOption?: string;

  /** The BE schema, once fetched; see `schemaDocument()`. */
  private schemaRequest?: Promise<SchemaDocument>;

  /** Binds every method of the class chain, subclass included, to put its call's `key` on the returned promise. */
  protected constructor(options: ProxyBaseOptions) {
    super(options.basePath);
    this.pkFieldName = options.pkFieldName;
    this.schemaValidationEnabled = options.validateSchema !== false;
    this.declaredMixins = options.declares;
    this.repeatOptions = options.repeat;
    this.eventClientOption = options.events;
    this.topicOption = options.topic;
    bindCallKeys(this, Object.prototype);
  }

  /**
   * Starts the advisory schema check. Subclasses call this as the last statement of their constructor:
   * during `super()` `request()` reads unassigned fields, and the check swallows its own errors.
   */
  protected initSchemaValidation(): void {
    if (this.schemaValidationEnabled) void this.validateAgainstSchema();
  }

  /** Abstract here, where a transport is actually held to it. See ViewSetInternals.request. */
  protected abstract override request<R>(method: HttpMethod, path: string, options?: RequestOptions): ViewSetCall<R>;

  /**
   * Compares the BE schema, fetched over this proxy's own transport, against `static declares` (every
   * action exists as a method on this class) and logs a console warning per mismatch. Errors are
   * ignored.
   */
  private async validateAgainstSchema(): Promise<void> {
    const declared = declaredActions(this, this.declaredMixins);
    if (declared === null) return;

    try {
      const paths = (await this.schemaDocument({ repeat: { withinMs: 0 }, background: true }))?.paths ?? {};
      const base = schemaBase(paths, this.basePath);

      const beActions = new Set<string>();
      const beAlternatives: { endpoint: string; actions: readonly string[] }[] = [];
      const customEndpoints: { endpoint: string; action: string }[] = [];

      for (const [path, pathItem] of Object.entries(paths)) {
        const suffix = path.slice(base.length).replace(/^\//, '');
        const verbs = Object.keys(pathItem).filter((v) => HTTP_METHODS.has(v.toLowerCase()));

        let pathType: string;
        if (suffix === '') {
          pathType = 'base';
        } else if (suffix === 'bulk') {
          pathType = 'bulk';
        } else if (suffix === 'lookup') {
          pathType = 'lookup';
        } else if (suffix === 'schema') {
          continue;
        } else if (suffix.startsWith('{') && !suffix.includes('/')) {
          pathType = 'pk';
        } else {
          // A custom endpoint, named by the operation's `x-viewset-action`; one without it is not checked.
          for (const verb of verbs) {
            const action = (pathItem[verb] as { [ACTION_EXTENSION]?: unknown } | undefined)?.[ACTION_EXTENSION];
            if (typeof action !== 'string' || !action) continue;
            customEndpoints.push({ endpoint: `${verb.toUpperCase()} ${path}`, action });
          }
          continue;
        }

        const methodMap = ENDPOINT_TO_FE_METHOD[pathType] ?? {};
        for (const verb of verbs) {
          const actions = methodMap[verb.toUpperCase()];
          if (!actions) continue;
          beAlternatives.push({ endpoint: `${verb.toUpperCase()} ${path}`, actions });
          for (const action of actions) beActions.add(action);
        }
      }

      const warnings: string[] = [];

      for (const action of STANDARD_FE_METHODS) {
        if (declared.has(action) && !beActions.has(action)) {
          warnings.push(`declares '${action}' but the BE viewset serves no such endpoint`);
        }
      }

      for (const { endpoint, actions } of beAlternatives) {
        if (!actions.some((action) => declared.has(action))) {
          warnings.push(`BE serves '${endpoint}' but the ViewSet declares no ${actions.join(' / ')}`);
        }
      }

      for (const { endpoint, action } of customEndpoints) {
        const names = customMethodNames(action);
        if (!names.some((name) => typeof (this as unknown as Record<string, unknown>)[name] === 'function')) {
          warnings.push(`BE serves '${endpoint}' but the ViewSet has no '${names.join("()' or '")}()' method`);
        }
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

  /**
   * The BE viewset's `/schema`, fetched over this proxy's transport on the first call and shared by
   * every later one. A failed fetch is not kept, so the next call fetches again. `repeat` and
   * `background` apply only to a call that issues the request.
   */
  private schemaDocument(options: Pick<RequestOptions, 'repeat' | 'background'>): Promise<SchemaDocument> {
    if (!this.schemaRequest) {
      const request = Promise.resolve(this.request<SchemaDocument>('GET', '/schema', options));
      this.schemaRequest = request;
      request.catch(() => {
        if (this.schemaRequest === request) this.schemaRequest = undefined;
      });
    }
    return this.schemaRequest;
  }

  /** See `ListDefaultSort.defaultSort`. */
  async defaultSort(): Promise<SortState> {
    const document = await this.schemaDocument({}).catch(() => this.schemaDocument({}));
    const paths = document?.paths ?? {};
    const base = schemaBase(paths, this.basePath);
    // A viewset routed at base_path "" lists its collection as '/', next to '/schema'.
    const operation = (paths[base] ?? paths[`${base}/`])?.get as
      { parameters?: { name?: unknown; in?: unknown; schema?: { default?: unknown } }[] } | undefined;
    const sort = operation?.parameters?.find((parameter) => parameter.name === 'sort' && parameter.in === 'query');
    const declared = sort?.schema?.default;
    return typeof declared === 'string' ? parseSort(declared) : [];
  }

  /**
   * The client `watch()` subscribes on: `ProxyBaseOptions.events`. Throws without it; the muxws
   * proxy falls back to its peer's client.
   */
  protected eventClient(): EventClient | Promise<EventClient> {
    if (this.eventClientOption) return this.eventClientOption;
    throw new Error(
      `[ViewSet ${this.basePath}] watch() needs an event client: pass \`events\` in the ViewSet's options`,
    );
  }

  /**
   * Subscribes to `records_changed` on `options.topic`, else `ProxyBaseOptions.topic`, else `basePath`.
   * With `fetch`, a change without `record` is completed by `retrieve(pk)` (`fetch: true`) or the
   * `fetch` function, as background calls.
   */
  watch(options: WatchOptions<T, K>): EventSubscription {
    return watchRecords<T, K>(
      {
        topic: options.topic ?? this.topicOption ?? this.basePath,
        pkFieldName: this.pkFieldName,
        retrieve: (pk) => this.retrieve(pk),
      },
      this.eventClient(),
      options,
    );
  }

  create(data: Omit<T, PK>): ViewSetActionCall<T> {
    return this.request<T>('POST', '', { body: data });
  }

  bulkCreate(data: Omit<T, PK>[]): ViewSetActionCall<T[]> {
    return this.request<T[]>('POST', '/bulk', { body: data });
  }

  /**
   * Requests `shape` with `X-List-Shape` and verifies that the BE answered in it; see
   * ListShapeMismatchError.
   */
  private async requestList<R>(shape: ListShape, query?: QueryParams): Promise<R> {
    const body = await this.request<unknown>('GET', '', { query, headers: { 'X-List-Shape': shape } });
    const received = listShapeOf(body);
    if (received !== shape) throw new ListShapeMismatchError(shape, received);
    return body as R;
  }

  list(params?: ListParams): ViewSetActionCall<T[]> {
    return this.requestList<T[]>('plain', params);
  }

  /** Fetches one cursor page from a BE viewset serving the `cursor` list shape; follow `next` to walk forward. */
  listCursor(params?: CursorParams): ViewSetActionCall<CursorPage<T>> {
    return this.requestList<{
      results: T[];
      limit: number;
      has_more: boolean;
      has_previous: boolean;
      next: string | null;
      previous: string | null;
      first: string | null;
      last: string | null;
    }>('cursor', params).then((page) => ({
      results: page.results,
      limit: page.limit,
      hasMore: page.has_more,
      hasPrevious: page.has_previous,
      next: page.next,
      previous: page.previous,
      first: page.first,
      last: page.last,
    }));
  }

  /**
   * Fetches one page from a BE viewset serving the `paginated` list shape. Only the envelope's fields
   * are renamed to camelCase; records pass through untouched.
   */
  listPage(params?: PageParams): ViewSetActionCall<PaginatedList<T>> {
    return this.requestList<{
      results: T[];
      offset: number;
      limit: number | null;
      count: number | null;
      has_more: boolean;
      has_previous: boolean;
    }>('paginated', params).then((page) => ({
      results: page.results,
      offset: page.offset,
      limit: page.limit,
      count: page.count,
      hasMore: page.has_more,
      hasPrevious: page.has_previous,
    }));
  }

  retrieve(pk: K): ViewSetActionCall<T> {
    return this.request<T>('GET', `/${pk}`);
  }

  update(pk: K, data: T): ViewSetActionCall<T> {
    return this.request<T>('PUT', `/${pk}`, { body: data });
  }

  partialUpdate(pk: K, data: Partial<T>): ViewSetActionCall<T> {
    return this.request<T>('PATCH', `/${pk}`, { body: data });
  }

  bulkUpdate(records: Record<K, T>): ViewSetActionCall<T[]> {
    return this.request<T[]>('PUT', '/bulk', { body: records });
  }

  bulkPartialUpdate(records: Record<K, Partial<T>>): ViewSetActionCall<T[]> {
    return this.request<T[]>('PATCH', '/bulk', { body: records });
  }

  destroy(pk: K): ViewSetActionCall<DestroyReturnData> {
    return this.request<DestroyReturnData>('DELETE', `/${pk}`);
  }

  bulkDestroy(pks: K[]): ViewSetActionCall<DestroyReturnData[]> {
    return this.request<DestroyReturnData[]>('DELETE', '/bulk', { body: pks });
  }

  /** `I` is the BE viewset's lookup item type; a factory-built ViewSet takes it from `restViewSet<T, L>()`. */
  lookup<I = LookupItem>(params?: LookupParams): ViewSetActionCall<I[]> {
    return this.request<I[]>('GET', '/lookup', { query: params });
  }
}
