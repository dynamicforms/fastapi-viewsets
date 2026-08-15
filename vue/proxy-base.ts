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
  ActionName,
  BulkViewSetMixin,
  CursorListMixin,
  CursorPage,
  CursorParams,
  DestroyReturnData,
  KeyType,
  ListParams,
  LookupItem,
  LookupMixin,
  PageParams,
  PaginatedList,
  PaginatedListMixin,
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
 * Maps (path type, HTTP method) → the actions that endpoint can satisfy.
 * Path types: 'base' = root, 'pk' = /{pk}, 'bulk' = /bulk, 'lookup' = /lookup.
 *
 * A list is a list of alternatives rather than one name because `GET {base}` is a single BE
 * endpoint that answers in whichever shape the viewset declared - see list_shapes.py. Declaring
 * `listCursor` and being served `GET {base}` is agreement, not a mismatch.
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

/** All standard FE method names, in a stable order for warning output. */
const STANDARD_FE_METHODS: readonly ActionName[] = [
  'list',
  'listPage',
  'listCursor',
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

/** One entry of a ViewSet's `static declares` list: a mixin naming the actions it contributes. */
export interface ViewSetMixinDeclaration {
  readonly actions: readonly string[];
}

/**
 * Which actions this ViewSet claims to have, from the `static declares` list on its class.
 *
 * Ordinary static lookup, so a subclass that declares its own list replaces its parent's rather
 * than adding to it - which is what a subclass pointed at a smaller BE viewset means. The new list
 * still has to be assignable to the parent's, since TypeScript checks a subclass's static side
 * against its base; a list the parent's type does not cover needs its own ViewSet.
 *
 * A ViewSet that declares nothing yields null rather than an empty set: "said nothing" and "said
 * it has no actions" are different claims, and only the second is worth checking against.
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
  /**
   * The mixins the ViewSet declares, for the `route_rest` / `route_muxws` path: those build a bare
   * proxy and use the ViewSet class only for typing, so a `static declares` on it would otherwise
   * never reach the object being checked.
   */
  declares?: readonly ViewSetMixinDeclaration[];
}

/**
 * What a ViewSet's own methods may reach - everything a custom endpoint needs, and nothing a caller
 * does.
 *
 * A real base class rather than a type, because `protected` is checked nominally: a subclass body
 * reaches `request()` only if it genuinely descends from the class that declared it. The ViewSet
 * factory hands back a class typed as this plus the declared actions, which is how a factory-built
 * ViewSet ends up with a narrow public surface and a usable private one.
 */
export abstract class ViewSetInternals {
  protected readonly basePath: string;

  protected constructor(basePath: string) {
    this.basePath = basePath.replace(/\/$/, '');
  }

  /**
   * Sends one request and returns the decoded response body.
   *
   * `path` is relative to `basePath` — '' for the collection, '/1' for a record, '/bulk', and so
   * on. Implementations must throw on a non-2xx status, with `response.status` readable on the
   * thrown value.
   *
   * Concrete here, and never reached: ViewSetProxyBase re-declares it abstract, which is where a
   * transport is actually held to implementing it. It cannot be abstract at this level because
   * TypeScript propagates an abstract member through a constructor type, and the factory hands one
   * back - every ViewSet a consumer wrote would then fail TS2515 for a method the transport
   * underneath it has always implemented.
   */
  // eslint-disable-next-line @typescript-eslint/no-unused-vars -- the signature is the point; the body cannot run
  protected request<R>(method: HttpMethod, path: string, options?: RequestOptions): Promise<R> {
    throw new Error('ViewSetInternals.request: no transport');
  }
}

export abstract class ViewSetProxyBase<K extends KeyType, T, PK extends keyof T>
  extends ViewSetInternals
  implements BulkViewSetMixin<K, T, PK>, CursorListMixin<T>, PaginatedListMixin<T>, LookupMixin
{
  /**
   * The mixins this ViewSet is composed of — the FE counterpart of a BE viewset's base classes.
   *
   *     class ItemViewSet extends RestProxyImpl<number, Item, 'id'> {
   *       static declares = [ReadOnlyViewSetMixin, LookupMixin];
   *     }
   *
   * Left undefined, the ViewSet is not checked against the BE schema at all. That is deliberate:
   * a ViewSet that never said what it has cannot be caught contradicting itself, and guessing on
   * its behalf is what made the check report actions nobody ever claimed.
   */
  static declares?: readonly ViewSetMixinDeclaration[];

  protected readonly pkFieldName: string;

  private readonly schemaValidationEnabled: boolean;

  private readonly declaredMixins?: readonly ViewSetMixinDeclaration[];

  protected constructor(options: ProxyBaseOptions) {
    super(options.basePath);
    this.pkFieldName = options.pkFieldName;
    this.schemaValidationEnabled = options.validateSchema !== false;
    this.declaredMixins = options.declares;
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

  /** Abstract here, where a transport is actually held to it. See ViewSetInternals.request. */
  protected abstract override request<R>(method: HttpMethod, path: string, options?: RequestOptions): Promise<R>;

  /**
   * Fetches the BE schema and compares it against what this ViewSet declared.
   * Logs a console warning for any mismatch found.
   *
   * The comparison is against `static declares`, not against which methods exist on the object:
   * every action is implemented unconditionally on this class, so `typeof this[action]` is true for
   * every ViewSet and answers a question nobody asked. `declares` is the only place the FE says
   * anything a BE viewset could disagree with.
   *
   * Non-critical: errors during fetch or parsing are silently ignored. Note that the schema is
   * fetched over this proxy's own transport, so a muxws proxy validates against the muxws
   * endpoint set and a REST proxy against the REST one — which is the point, since the two are
   * allowed to differ.
   */
  private async validateAgainstSchema(): Promise<void> {
    // A ViewSet that declares nothing is not checked, and does not pay for the schema request
    // either. The alternative - assuming it meant "all of them" - is what the previous
    // implementation effectively did, and it is why a viewset built from one mixin reported every
    // action it had never claimed to have.
    const declared = declaredActions(this, this.declaredMixins);
    if (declared === null) return;

    try {
      const schema = await this.request<{ paths?: Record<string, Record<string, unknown>> }>('GET', '/schema');
      const paths = schema?.paths ?? {};

      const beActions = new Set<string>();
      const beAlternatives: { endpoint: string; actions: readonly string[] }[] = [];
      const customEndpoints: { endpoint: string; method: string }[] = [];

      for (const [path, pathItem] of Object.entries(paths)) {
        const suffix = path.slice(this.basePath.length).replace(/^\//, '');
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
          // A custom endpoint. Here - and only here - asking whether the proxy has a method of that
          // name is honest: the base implements no custom actions, so whatever answers is something
          // this ViewSet's own author wrote.
          for (const verb of verbs) {
            customEndpoints.push({ endpoint: `${verb.toUpperCase()} ${path}`, method: suffix.split('/')[0] });
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

      for (const { endpoint, method } of customEndpoints) {
        if (typeof (this as unknown as Record<string, unknown>)[method] !== 'function') {
          warnings.push(`BE serves '${endpoint}' but the ViewSet has no '${method}()' method`);
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
  /**
   * Fetches one cursor page. Only meaningful against a viewset built on the BE CursorListMixin.
   *
   * Follow `next` to walk forward. Unlike offset paging, a row inserted or removed behind you
   * cannot make the next page repeat or skip anything.
   */
  async listCursor(params?: CursorParams): Promise<CursorPage<T>> {
    const page = await this.request<{
      results: T[];
      limit: number;
      has_more: boolean;
      has_previous: boolean;
      next: string | null;
      previous: string | null;
      first: string | null;
      last: string | null;
    }>('GET', '', { query: params });
    return {
      results: page.results,
      limit: page.limit,
      hasMore: page.has_more,
      hasPrevious: page.has_previous,
      next: page.next,
      previous: page.previous,
      first: page.first,
      last: page.last,
    };
  }

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
