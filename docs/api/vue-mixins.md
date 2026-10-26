# Vue Mixins — API Reference

```ts
import { ... } from '@dynamicforms/fastapi-viewsets';
```

Each mixin is an interface merged with a class of the same name. The interface names the actions and their signatures;
the class carries `static readonly actions`, the same statement in a form that survives to runtime. A ViewSet
declaration lists the mixin classes themselves: the list types the ViewSet's public surface, and is what the schema
check compares against the BE.

The members are methods, not function-valued properties, so a ViewSet may override an action with a method of its own.
An action returns a [`ViewSetActionCall<R>`](./idempotency#frontend): a `Promise<R>` whose `key` is
optional in the type, so an override may return a plain `Promise<R>`.

## Types

```ts
type KeyType = string | number;
type DestroyReturnData = Record<KeyType, any>;

interface LookupItem {
  group: unknown;
  pk: unknown;
  title: string;
  icon: string | null;
}

/** `q` and the other fields of the BE lookup filter model. */
interface LookupParams {
  q?: string;
  [key: string]: string | number | boolean | null | undefined | Array<string | number>;
}

/**
 * `sort` is 'column:asc,other:desc'; the rest are filters. Without `sort` the BE applies its
 * `default_sort`; '' leaves the list unordered.
 */
interface ListParams {
  sort?: string;
  [key: string]: string | number | boolean | null | undefined | Array<string | number>;
}

/** The shape of `@dynamicforms/vue-grid`'s `SortStateColumn`: a `SortState` is a grid's `sortState`. */
interface SortStateColumn {
  columnName: string;
  direction: 'asc' | 'desc';
}
type SortState = SortStateColumn[];

interface PageParams extends ListParams {
  offset?: number;
  limit?: number;
}

interface PaginatedList<T> {
  results: T[];
  offset: number;
  limit: number | null;
  count: number | null;
  hasMore: boolean;
  hasPrevious: boolean;
}

interface CursorParams extends ListParams {
  cursor?: string;
  limit?: number;
}

interface CursorPage<T> {
  results: T[];
  limit: number;
  hasMore: boolean;
  hasPrevious: boolean;
  next: string | null;
  previous: string | null;
  first: string | null;
  last: string | null;
}
```

### List types

```ts
/** The row types a BE viewset's list adds through `MultiTypeListMixin`, as `{ wireName: RowType }`; `{}` for none. */
type ListTypes = object;

/** The wire names of the list types `V`: the values `ListOptions.listType` accepts. */
type ListTypeName<V extends ListTypes> = keyof V & string;

/** The row type of a list call that sends `listType` `N`: `V[N]`, or `T` when `V` names no type. */
type ListRow<T, V extends ListTypes, N extends ListTypeName<V>> = [keyof V] extends [never] ? T : V[N];

/** The row type of a list call whose `listType` may be absent: `T` or any of `V`'s row types. */
type AnyListRow<T, V extends ListTypes> = T | V[ListTypeName<V>];

/** The options of `list()`, `listPage()` and `listCursor()`. */
interface ListOptions<N extends string = string> {
  /** Sent as `X-List-Type`; without it the header is not sent and the BE answers in its default row type. */
  listType?: N;
}

/** The options of a list call that selects the row type `N`: `listType` is required. */
interface ListTypeOptions<N extends string> extends ListOptions<N> {
  listType: N;
}

/** The row types of a list mixin, `T` without `listType` and `V` by wire name. A type-level member only. */
interface ListRows<T, V extends ListTypes> {
  readonly [LIST_ROWS]?: { readonly row: T; readonly types: V };
}

/** The `ListRows` member of `M`, `{ row, types }`; `never` when `M` has none. */
type ListRowsOf<M> = typeof LIST_ROWS extends keyof M ? NonNullable<M[typeof LIST_ROWS]> : never;
```

`ListRows` is a phantom member: no ViewSet object carries it at runtime. `ViewSetCalls`, `RowOf`
and `RecordOf` read the row types off it, since inferring a type from an overloaded method sees its
last overload only. A mixin interface without it - one written by hand, `list(): Promise<Item[]>` -
is read off its methods' return types. `ActionName` excludes its key.

### Requests and actions

```ts
type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

/** Query values; an array becomes a repeated key, which is how FastAPI binds `list[str]`. */
type QueryParams = Record<string, string | number | boolean | null | undefined | Array<string | number>>;

interface RequestOptions {
  query?: QueryParams;
  body?: unknown;
  /** Headers for this call only; on muxws they override the proxy's own `headers`. */
  headers?: Record<string, string>;
  /** The Idempotency-Key of this call; a new UUID when absent. Overrides an Idempotency-Key in `headers`. */
  key?: string;
  /** Overrides the proxy's `repeat` for this call. */
  repeat?: RepeatOptions;
  /** Reported to `onCallActivity` listeners as `background: true`; default false. See also `inBackground`. */
  background?: boolean;
}

/** The response shapes of `GET {basePath}`, as the BE names them in `X-List-Shape`. */
type ListShape = 'plain' | 'paginated' | 'cursor';

/** One entry of a ViewSet's `declares`: a mixin naming the actions it contributes. */
interface ViewSetMixinDeclaration {
  readonly actions: readonly string[];
}

/** Every action a mixin can name: 'list', 'listPage', 'listCursor', 'create', 'retrieve', ... */
type ActionName = Exclude<keyof ActionSurface<KeyType, unknown, never, string>, typeof LIST_ROWS>;

/**
 * The members of the mixins whose actions are in `A`, intersected: the public surface of a
 * factory-built ViewSet. `L` is the lookup item type; `V` and `R` are the list's row types.
 */
type ActionSurface<
  K extends KeyType,
  T,
  PK extends keyof T,
  A,
  L = LookupItem,
  V extends ListTypes = {},
  R = T,
> = ('create' extends A ? CreateMixin<T, PK> : unknown) &
  ('bulkCreate' extends A ? BulkOnlyCreateMixin<T, PK> : unknown) &
  ('list' extends A ? ListMixin<R, V> : unknown) &
  ('listPage' extends A ? PaginatedListMixin<R, V> : unknown) &
  ('listCursor' extends A ? CursorListMixin<R, V> : unknown) &
  ('retrieve' extends A ? RetrieveMixin<K, T> : unknown) &
  ('update' extends A ? UpdateMixin<K, T> : unknown) &
  ('bulkUpdate' extends A ? BulkOnlyUpdateMixin<K, T> : unknown) &
  ('destroy' extends A ? DestroyMixin<K> : unknown) &
  ('bulkDestroy' extends A ? BulkOnlyDestroyMixin<K> : unknown) &
  ('lookup' extends A ? LookupMixin<L> : unknown);
```

`request<R>(method: HttpMethod, path: string, options?: RequestOptions): ViewSetCall<R>` is the one
call a transport implements; see [custom endpoints](../guide/vue-custom-endpoints#this-request).
`ViewSetCall`, `ViewSetActionCall` and `RepeatOptions` are in [Idempotency](./idempotency#frontend).

## watch()

Every ViewSet has `watch()`, whichever mixins it declares: a factory-built one, a `route_rest` or
`route_muxws` proxy, and a class extending `RestProxyImpl` or `MuxwsProxyImpl`. The type of a
`route_rest` proxy built from positional arguments, which take no `events`, carries no `watch()`.

```ts
interface ViewSetWatch<T, K extends KeyType = KeyType, F = boolean | FetchRows<T, K>> {
  watch(options: WatchOptions<T, K> & { fetch?: F }): EventSubscription;
}

interface WatchOptions<T, K extends KeyType = KeyType> {
  onChange: (change: RecordsChanged<T, K>, meta: EventMeta) => void;
  onResync: () => void;
  fetch?: boolean | FetchRows<T, K>;
  onError?: (error: unknown) => void;
  topic?: string;
}

/** The rows of `pks`, in any order. */
type FetchRows<T, K extends KeyType = KeyType> = (pks: K[]) => Promise<readonly T[]>;

/**
 * `boolean | FetchRows<R, K>` when `R`, the row type of a list call without `listType`, is the record
 * type `T`; `false | FetchRows<R, K>` otherwise.
 */
type FetchOption<R, T, K extends KeyType = KeyType> = /* ... */;

/** The record type a mixin interface `M` names, read off its actions; `Record<string, unknown>` for none. */
type RecordOf<M> = M extends { retrieve(...args: any[]): Promise<infer T> }
  ? T
  : [ListRowsOf<M>] extends [never]
    ? RecordFromActions<M> // the row type of list(), else create(), listPage(), listCursor(); else Record<string, unknown>
    : ListRowsOf<M>['row'];

/** The row type of a list call of `M` without `listType`: its `ListRows` row type, else `RecordOf<M>`. */
type RowOf<M> = [ListRowsOf<M>] extends [never] ? RecordOf<M> : ListRowsOf<M>['row'];

/** The pk type a mixin interface `M` names, read off its actions; `KeyType` for none. */
type KeyOf<M> = M extends { retrieve(pk: infer K): any }
  ? K & KeyType
  : M extends { destroy(pk: infer K): any }
    ? K & KeyType
    : M extends { update(pk: infer K, ...args: any[]): any }
      ? K & KeyType
      : KeyType;
```

`watch(options)` subscribes the ViewSet's topic, `options.topic`, else the ViewSet's `topic` option,
else its `basePath`, to `records_changed`
([`RECORDS_CHANGED`](./vue-events#records-changed)) and returns the
[`EventSubscription`](./vue-events#eventsubscription). The event client is `events` from the
ViewSet's options; without it, a muxws ViewSet uses `eventsOf(peer)`, and a REST ViewSet throws.

| Option | Meaning |
|--------|---------|
| `onChange` | Required. Called once per `records_changed` event of version `1` of the topic (with `fetch`, twice for an event some of whose records are gone), with its [`RecordsChanged`](./vue-events#records-changed) payload and its [`EventMeta`](./vue-events#eventmeta) |
| `onResync` | Required. Called on a resync of the topic, and for a `records_changed` event whose version is not `1`, with or without `fetch`: the caller re-reads what it displays |
| `fetch` | Completes every `created` and `updated` change without `record` before `onChange`, with calls issued inside [`inBackground`](./idempotency#frontend) so call activity reports them as background calls. `true`: one `retrieve(pk)` per change; a 404 means the record is gone, another error is passed to `onError` and the event ends in `onResync` instead of `onChange`. A `FetchRows` function: one call per event with the pks of those changes; each returned row is matched to its change by `pkFieldName` (pks compare by their string form), a pk without a row is gone, and a rejection is passed to `onError` and ends the event in `onResync`. The changes whose record is gone follow the event in a second `onChange` with `op: 'deleted'` and the same `meta`; when every record is gone only that one is called. Default `false` |
| `onError` | Called with the error of a refused `subscribe` or `poll` of the topic, of a failed `fetch: true` retrieve and of a rejected `fetch` function |
| `topic` | The client topic to subscribe: the BE path relative to the application's `root_path`, without a Starlette `Mount` prefix. Default the ViewSet's `topic` option, else its `basePath` |

`onChange` and `onResync` are called in the order their events arrived, also while `fetch` waits for
its calls. With `fetch`, `onChange` is called for an event whose changes were all dropped, with an
empty `changes`. After `unsubscribe()` of the returned subscription no option is called, also for an
event that arrived before it and whose `fetch` call is still in flight.

The callbacks are function-typed properties: a callback whose parameter is typed other than the
option's, a wider record type included, does not compile. An `onChange` that declares only the
change parameter compiles.

| Throws `Error` | When |
|----------------|------|
| ``[ViewSet {basePath}] watch() needs an event client: pass `events` in the ViewSet's options`` | A REST ViewSet without `events` |
| `[ViewSet {basePath}] watch() found no event client for the peer: createEventClient({ peer }) registers one` | A muxws ViewSet without `events` whose peer has no client. Before a function `peer` has resolved, `watch()` returns, and once the peer resolves `ready` rejects with the error and `onError` is called with it; without `onError` it is rethrown in a microtask |

`RestProxy<M>` and `MuxwsProxy<M>` carry `watch()` as
`ViewSetWatch<RowOf<M>, KeyOf<M>, FetchOption<RowOf<M>, RecordOf<M>, KeyOf<M>>>`, a factory-built
ViewSet as `ViewSetWatch<R, PkType<T, PK>, FetchOption<R, T, PkType<T, PK>>>`
([`PkType`](./viewset-factory#signatures)): the changes carry the rows of a list call without
`listType`, and `fetch: true`, which reads records, compiles only where those rows are records. A
mixin interface with list actions only names no separate record type, so `RecordOf<M>` is its row
type and `fetch: true` compiles for it. A class extending `RestProxyImpl` or
`MuxwsProxyImpl` carries `ViewSetWatch<T, K>`. `ViewSetWatch` also has the overload
`watch<Row>(options & { fetch: FetchRows<Row, K> })`: with a `fetch` function, `onChange` receives
the rows that function returns.
Applying the changes to a list of rows is [`applyChanges`](./vue-events#records-changed); the guide
is [`watch()` on a ViewSet](../guide/vue-events#watch-on-a-viewset).

## Errors

```ts
class ViewSetRequestError extends Error {
  readonly response: { status: number; data: unknown; headers: Record<string, string> };

  constructor(status: number, data: unknown, headers?: Record<string, string>);
}
```

`headers` are the reply headers with the `:`-prefixed pseudo-headers dropped and every value
stringified, `{}` when the constructor is not given any. `name` is `'ViewSetRequestError'` and the
message is `Request failed with status code {status}`.

```ts
class ListShapeMismatchError extends Error {
  readonly expected: ListShape;
  readonly received: ListShape | 'unknown';
}
```

Thrown by `list()`, `listPage()` and `listCursor()` when the reply is not the envelope the method
requested: `plain` is an array, `paginated` an object with `results` and `offset`, `cursor` an object
with `results`, `next` and `has_more`. `received` is `'unknown'` for a body that matches none of them.
`name` is `'ListShapeMismatchError'` and the message is
`Expected a '{expected}' list from the BE, received '{received}'`.

Which transport throws `ViewSetRequestError` and which throws axios' own `AxiosError`, and what a handler may read off
either, is in [handling a failed call](../guide/vue-mixins#handling-a-failed-call).

### Error bodies

```ts
/** The body of every error response of the BE. */
interface ApiErrorBody {
  detail: string;
  detail_code: string;
  detail_params: Record<string, unknown>;
  field_errors: Record<string, ApiFieldError[]>;
}

/** One failure of one field of a request that failed validation. */
interface ApiFieldError {
  code: string; // pydantic.<type>
  params: Record<string, unknown>;
  detail: string;
}

/** Assignable to vue-forms' `ErrorDescription`. */
interface ErrorDescription {
  readonly code: string;
  readonly params: Readonly<Record<string, unknown>>;
  readonly detail: string;
  readonly origin: 'server';
}

function isApiErrorBody(data: unknown): data is ApiErrorBody;
/** `error.response.data` when it is an `ApiErrorBody`, else `fastapi-viewsets.no_response` / `unexpected_response`. */
function errorBodyOf(error: unknown): ApiErrorBody;
function toErrorDescription(body: ApiErrorBody): ErrorDescription;
function toFieldErrors(body: ApiErrorBody): Record<string, ErrorDescription[]>;
```

What each conversion produces is in [Error codes → Frontend](../guide/error-codes#frontend).

## Individual operation mixins

### CreateMixin `<T, PK extends keyof T>`

```ts
create(data: Omit<T, PK>): ViewSetActionCall<T>;
```

### BulkOnlyCreateMixin `<T, PK extends keyof T>`

```ts
bulkCreate(data: Omit<T, PK>[]): ViewSetActionCall<T[]>;
```

### BulkCreateMixin `<T, PK extends keyof T>`

Interface extends `CreateMixin<T, PK>` and `BulkOnlyCreateMixin<T, PK>`; `actions` spreads both.

### ListMixin `<T, V = {}>`

```ts
list(params?: ListParams, options?: ListOptions<never>): ViewSetActionCall<T[]>;
list<N extends ListTypeName<V>>(
  params: ListParams | undefined,
  options: ListTypeOptions<N>,
): ViewSetActionCall<ListRow<T, V, N>[]>;
list(params?: ListParams, options?: ListOptions<ListTypeName<V>>): ViewSetActionCall<AnyListRow<T, V>[]>;
```

### PaginatedListMixin `<T, V = {}>`

```ts
listPage(params?: PageParams, options?: ListOptions<never>): ViewSetActionCall<PaginatedList<T>>;
listPage<N extends ListTypeName<V>>(
  params: PageParams | undefined,
  options: ListTypeOptions<N>,
): ViewSetActionCall<PaginatedList<ListRow<T, V, N>>>;
listPage(params?: PageParams, options?: ListOptions<ListTypeName<V>>): ViewSetActionCall<PaginatedList<AnyListRow<T, V>>>;
```

### CursorListMixin `<T, V = {}>`

```ts
listCursor(params?: CursorParams, options?: ListOptions<never>): ViewSetActionCall<CursorPage<T>>;
listCursor<N extends ListTypeName<V>>(
  params: CursorParams | undefined,
  options: ListTypeOptions<N>,
): ViewSetActionCall<CursorPage<ListRow<T, V, N>>>;
listCursor(params?: CursorParams, options?: ListOptions<ListTypeName<V>>): ViewSetActionCall<CursorPage<AnyListRow<T, V>>>;
```

Each interface extends `ListRows<T, V>`. `T` is the row type of a call without `listType`; `V` maps
the wire name of every row type the BE viewset adds with
[`MultiTypeListMixin`](./python-mixins#multitypelistmixin-ts) to that type. A call with
`{ listType: 'compact' }` sends `X-List-Type: compact` and answers with `V['compact']`; a `listType`
that is not a key of `V` does not compile. A call without `listType` sends no `X-List-Type` and
answers with `T`. A call whose options are typed `ListOptions<...>`, so that `listType` may be
absent, answers with `AnyListRow<T, V>`. An explicit type argument without `listType` in the
options (`list<'compact'>()`) does not compile.

The three methods read the same `GET {basePath}`. Each sends `X-List-Shape` with its own shape - `plain`, `paginated`,
`cursor` - and throws `ListShapeMismatchError` when the reply is in another one. A BE viewset that declares one shape
does not read the header and answers in that shape, so a ViewSet declares the mixins of the shapes the BE viewset offers
through `list_shape` and `list_shapes`. A shape the BE viewset's `list_shapes` leave out is a 422
`fastapi-viewsets.unsupported_list_shape`.

A row type the BE viewset does not offer is a 422 `fastapi-viewsets.validation_failed` on the
`X-List-Type` header. The composites and `restViewSet<T, L, V, R>()` / `muxwsViewSet<T, L, V, R>()`
pass their `R` and `V` to the three mixins. On `ViewSetProxyBase` the methods are
`list(params?: ListParams, options?: ListOptions): ViewSetActionCall<T[]>`,
`listPage(params?: PageParams, options?: ListOptions)` and
`listCursor(params?: CursorParams, options?: ListOptions)`, with rows typed `T`.

### ListDefaultSort

A ViewSet declaring any of the three list mixins has `defaultSort()` as well. It is not an action and
is not in `ActionName`:

```ts
interface ListDefaultSort {
  defaultSort(): Promise<SortState>;
}
```

`defaultSort()` resolves to the BE viewset's `default_sort`, `[]` when it declares none. It reads the
`sort` parameter's default of `GET {base}` in the viewset's `/schema`, fetched over the ViewSet's
transport once per instance and shared with the startup schema check. When the shared request fails,
`defaultSort()` issues one of its own, repeated as any other call is; when that fails too, it rejects,
and the next call fetches again. `ViewSetProxyBase` implements it, so a hand-written proxy
has it too; `RestProxy<M>` and `MuxwsProxy<M>` carry it when `M` has `list`, `listPage` or
`listCursor` (`DefaultSortOf<keyof M>`).

`parseSort(sort: string | null | undefined): SortState` reads a `sort` value as the BE does: a
missing direction is `asc`, and an empty entry or one with any other direction is skipped.
`formatSort(state: readonly SortStateColumn[]): string` writes one, `''` for an empty state.

### RetrieveMixin `<K extends KeyType, T>`

```ts
retrieve(pk: K): ViewSetActionCall<T>;
```

### UpdateMixin `<K extends KeyType, T>`

```ts
update(pk: K, data: T): ViewSetActionCall<T>;
partialUpdate(pk: K, data: Partial<T>): ViewSetActionCall<T>;
```

### BulkOnlyUpdateMixin `<K extends KeyType, T>`

```ts
bulkUpdate(records: Record<K, T>): ViewSetActionCall<T[]>;
bulkPartialUpdate(records: Record<K, Partial<T>>): ViewSetActionCall<T[]>;
```

### BulkUpdateMixin `<K extends KeyType, T>`

Interface extends `UpdateMixin<K, T>` and `BulkOnlyUpdateMixin<K, T>`; `actions` spreads both.

### DestroyMixin `<K extends KeyType>`

```ts
destroy(pk: K): ViewSetActionCall<DestroyReturnData>;
```

### BulkOnlyDestroyMixin `<K extends KeyType>`

```ts
bulkDestroy(pks: K[]): ViewSetActionCall<DestroyReturnData[]>;
```

### BulkDestroyMixin `<K extends KeyType>`

Interface extends `DestroyMixin<K>` and `BulkOnlyDestroyMixin<K>`; `actions` spreads both.

### LookupMixin `<L = LookupItem>`

```ts
lookup(params?: LookupParams): ViewSetActionCall<L[]>;
```

An array in `params` goes out as a repeated key (`ids=1&ids=2`) on both transports. On
`ViewSetProxyBase` the method is `lookup<I = LookupItem>(params?: LookupParams): ViewSetActionCall<I[]>`.

## Combined viewset mixins

A composite restates no signature: its interface extends the leaves its `actions` names, so each action is written in
exactly one place.

`V` and `R` are the list's row types, `ListMixin`'s `V` and `T`: `R` is the row type of a list call
without `listType`, default `T`.

### ReadOnlyViewSetMixin `<K extends KeyType, T, V extends ListTypes = {}, R = T>`

Interface extends `ListMixin<R, V>` and `RetrieveMixin<K, T>`; `actions` spreads both.

### ViewSetMixin `<K extends KeyType, T, PK extends keyof T, V extends ListTypes = {}, R = T>`

Interface extends `ReadOnlyViewSetMixin<K, T, V, R>`, `CreateMixin<T, PK>`, `UpdateMixin<K, T>` and `DestroyMixin<K>`;
`actions` spreads all four.

### BulkViewSetMixin `<K extends KeyType, T, PK extends keyof T, V extends ListTypes = {}, R = T>`

Interface extends `ViewSetMixin<K, T, PK, V, R>`, `BulkOnlyCreateMixin<T, PK>`, `BulkOnlyUpdateMixin<K, T>` and
`BulkOnlyDestroyMixin<K>`; `actions` spreads all four.
