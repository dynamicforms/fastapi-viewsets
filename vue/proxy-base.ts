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
   * The `Idempotency-Key` of this call. Defaults to a new UUID. An application that repeats a call
   * itself passes the `key` of the first one. It overrides an `Idempotency-Key` in `headers`.
   */
  key?: string;
  /** Overrides the proxy's `repeat` for this call. */
  repeat?: RepeatOptions;
  /**
   * Reported to `onCallActivity` listeners as `background: true`, for a call a busy indicator leaves
   * out. Default false.
   */
  background?: boolean;
}

/** The response shapes of `GET {basePath}`, as the BE names them in `X-List-Shape`. */
export type ListShape = 'plain' | 'paginated' | 'cursor';

/**
 * Thrown by `list()`, `listPage()` and `listCursor()` when the BE answers in a shape other than the
 * one the method asked for. A BE viewset that declares a single shape does not read `X-List-Shape`
 * and answers in that shape; this error is what such a viewset produces for the other two methods.
 *
 * `received` is `'unknown'` when the body matches none of the three envelopes.
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

/**
 * The FE method names that serve the BE method `action`: its camelCase form, and the name itself
 * where that differs. `top_rated` is served by `topRated()` or `top_rated()`.
 */
function customMethodNames(action: string): string[] {
  const camel = action.replace(/(?<=[a-zA-Z0-9])_+([a-z0-9])/g, (_, char: string) => char.toUpperCase());
  return camel === action ? [action] : [camel, action];
}

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

/**
 * Every `ActionName` mapped to `true`, in a stable order for warning output. A `Record<ActionName,
 * true>` rather than a plain array: an action added to (or dropped from) the `ActionName` union
 * without a matching change here fails to compile - a missing key or an excess one - instead of
 * silently narrowing what the mismatch check below is able to report.
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
 * The viewset's base path as the schema document states it. Paths in the document are the viewset's
 * routes as declared, without the prefixes the application routes them under (include_router) and
 * with path parameters unsubstituted, so they share no common prefix with `basePath`. The base is
 * taken from the document's own /schema path; `basePath` is used only for a document that does not
 * list it.
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
 * Marks a class's `declares` as coming from the ViewSet factory (viewset.ts's `FactoryDeclares<D>`)
 * rather than being hand-written. Declared here, the module both viewset.ts and the two proxy
 * implementations already import from, so all three sides see the same `unique symbol` and a
 * structural check against it type-checks identically everywhere - `route_rest`/`route_muxws` use it
 * to reject a factory-built class at the call site (see rest-proxy.ts, muxws-proxy.ts), which would
 * otherwise type-check and hand back a bare proxy missing the class's own custom methods.
 */
export declare const FACTORY_BUILT: unique symbol;

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
   * Sends one call and returns the decoded response body, on a `ViewSetCall` carrying the call's
   * `Idempotency-Key`. The request of the first attempt is issued synchronously, so a method that
   * returns this promise before its first `await` returns a promise with the same key.
   *
   * `path` is relative to `basePath` — '' for the collection, '/1' for a record, '/bulk', and so
   * on. Implementations must throw on a status of 400 or above, with `response.status` readable on
   * the thrown value. Below 400 the two differ, on a band a caller rarely sees: the muxws proxy
   * returns the body for any 3xx, while the REST proxy follows a redirect it can follow and rejects
   * whatever axios' default `validateStatus` then leaves outside 200-299.
   *
   * Concrete here, and never reached: ViewSetProxyBase re-declares it abstract, which is where a
   * transport is actually held to implementing it. It cannot be abstract at this level because
   * TypeScript propagates an abstract member through a constructor type, and the factory hands one
   * back - every ViewSet a consumer wrote would then fail TS2515 for a method the transport
   * underneath it implements.
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
   * The mixins this ViewSet is composed of — the FE counterpart of a BE viewset's base classes.
   *
   *     class ItemViewSet extends RestProxyImpl<number, Item, 'id'> {
   *       static declares = [ReadOnlyViewSetMixin, LookupMixin];
   *     }
   *
   * Left undefined, the ViewSet is not checked against the BE schema at all: with no declared
   * mixins there is nothing to compare, and checking every standard action would report actions
   * the ViewSet does not have.
   */
  static declares?: readonly ViewSetMixinDeclaration[];

  /**
   * Which field of `T` is the primary key, e.g. `'id'`. `public`, not `protected`: nothing in this
   * library reads it back, but a generic caller holding a ViewSet instance - a grid or table
   * component needing to know which column identifies a row - has no other way to ask.
   */
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

  /**
   * Binds every method of the class chain to set the `key` of the call it issues on the promise it
   * returns (see call.ts's `bindCallKeys`). The methods are bound per instance, here, so a subclass's
   * own methods are bound as well.
   */
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
   * Starts the advisory schema check. Subclasses must call this as the *last* statement of their
   * constructor, never the base constructor itself: `request()` reads fields the subclass has not
   * assigned yet while `super()` is still running, and since the check swallows its own errors,
   * doing it here would leave it permanently and silently dead.
   */
  protected initSchemaValidation(): void {
    if (this.schemaValidationEnabled) void this.validateAgainstSchema();
  }

  /** Abstract here, where a transport is actually held to it. See ViewSetInternals.request. */
  protected abstract override request<R>(method: HttpMethod, path: string, options?: RequestOptions): ViewSetCall<R>;

  /**
   * Fetches the BE schema and compares it against what this ViewSet declared.
   * Logs a console warning for any mismatch found.
   *
   * The comparison is against `static declares`, not against which methods exist on the object:
   * every action is implemented unconditionally on this class, so `typeof this[action]` is true for
   * every ViewSet.
   *
   * Non-critical: errors during fetch or parsing are silently ignored. Note that the schema is
   * fetched over this proxy's own transport, so a muxws proxy validates against the muxws
   * endpoint set and a REST proxy against the REST one — which is the point, since the two are
   * allowed to differ.
   */
  private async validateAgainstSchema(): Promise<void> {
    // A ViewSet that declares nothing is not checked, and does not pay for the schema request
    // either. Assuming it meant "all of them" would report, for a viewset built from one mixin,
    // every action it never claimed to have.
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
          // A custom endpoint: the base implements no custom actions, so a method of that name is one
          // the ViewSet defines. The name is the BE method's, from the operation's
          // `x-viewset-action`; an operation without it is not checked.
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
   * Subscribes the ViewSet's topic to `records_changed`: `options.topic`, else
   * `ProxyBaseOptions.topic`, else `basePath`; see `WatchOptions`. With `fetch`, a change without
   * `record` is completed with `retrieve(pk)`, issued as a background call.
   */
  watch(options: WatchOptions<T, K>): EventSubscription {
    return watchRecords<T, K>(
      { topic: options.topic ?? this.topicOption ?? this.basePath, retrieve: (pk) => this.retrieve(pk) },
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

  /**
   * Fetches one cursor page, from a BE viewset whose `list_shape` is `cursor` or whose `list_shapes`
   * include it.
   *
   * Follow `next` to walk forward. Unlike offset paging, a row inserted or removed behind you
   * cannot make the next page repeat or skip anything.
   */
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
   * Fetches one page, from a BE viewset whose `list_shape` is `paginated` or whose `list_shapes`
   * include it.
   *
   * The BE speaks snake_case (`has_more`); the rest of this client speaks whatever the model
   * declares, so only the envelope's own fields are renamed here. The records inside are passed
   * through untouched.
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
