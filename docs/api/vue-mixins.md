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

/** `sort` is 'column:asc,other:desc'; the rest are filters. */
interface ListParams {
  sort?: string;
  [key: string]: string | number | boolean | null | undefined | Array<string | number>;
}

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
type ActionName = keyof ActionSurface<KeyType, unknown, never, string>;

/** The members of the mixins whose actions are in `A`, intersected: the public surface of a factory-built ViewSet. */
type ActionSurface<K extends KeyType, T, PK extends keyof T, A> = ('create' extends A
  ? CreateMixin<T, PK>
  : unknown) &
  ('bulkCreate' extends A ? BulkOnlyCreateMixin<T, PK> : unknown) &
  ('list' extends A ? ListMixin<T> : unknown) &
  ('listPage' extends A ? PaginatedListMixin<T> : unknown) &
  ('listCursor' extends A ? CursorListMixin<T> : unknown) &
  ('retrieve' extends A ? RetrieveMixin<K, T> : unknown) &
  ('update' extends A ? UpdateMixin<K, T> : unknown) &
  ('bulkUpdate' extends A ? BulkOnlyUpdateMixin<K, T> : unknown) &
  ('destroy' extends A ? DestroyMixin<K> : unknown) &
  ('bulkDestroy' extends A ? BulkOnlyDestroyMixin<K> : unknown) &
  ('lookup' extends A ? LookupMixin : unknown);
```

`request<R>(method: HttpMethod, path: string, options?: RequestOptions): ViewSetCall<R>` is the one
call a transport implements; see [custom endpoints](../guide/vue-custom-endpoints#this-request).
`ViewSetCall`, `ViewSetActionCall` and `RepeatOptions` are in [Idempotency](./idempotency#frontend).

## watch()

Every ViewSet has `watch()`, whichever mixins it declares: a factory-built one, a `route_rest` or
`route_muxws` proxy, and a class extending `RestProxyImpl` or `MuxwsProxyImpl`. The type of a
`route_rest` proxy built from positional arguments, which take no `events`, carries no `watch()`.

```ts
interface ViewSetWatch<T, K extends KeyType = KeyType> {
  watch(options: WatchOptions<T, K>): EventSubscription;
}

interface WatchOptions<T, K extends KeyType = KeyType> {
  onChange: (change: RecordsChanged<T, K>, meta: EventMeta) => void;
  onResync: () => void;
  fetch?: boolean;
  onError?: (error: unknown) => void;
  topic?: string;
}

/** The record type a mixin interface `M` names, read off its actions; `Record<string, unknown>` for none. */
type RecordOf<M> = M extends { retrieve(...args: any[]): Promise<infer T> }
  ? T
  : M extends { list(...args: any[]): Promise<(infer T)[]> }
    ? T
    : M extends { create(...args: any[]): Promise<infer T> }
      ? T
      : M extends { listPage(...args: any[]): Promise<PaginatedList<infer T>> }
        ? T
        : M extends { listCursor(...args: any[]): Promise<CursorPage<infer T>> }
          ? T
          : Record<string, unknown>;

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
| `onChange` | Required. Called once per `records_changed` event of version `1` of the topic (with `fetch`, twice for an event some of whose retrieves answer 404), with its [`RecordsChanged`](./vue-events#records-changed) payload and its [`EventMeta`](./vue-events#eventmeta) |
| `onResync` | Required. Called on a resync of the topic, and for a `records_changed` event whose version is not `1`, with or without `fetch`: the caller re-reads what it displays |
| `fetch` | `true`: every `created` and `updated` change without `record` is completed with `retrieve(pk)`, issued inside [`inBackground`](./idempotency#frontend) so call activity reports it as a background call, before `onChange`, and the changes whose retrieve answers 404 follow the event in a second `onChange` with `op: 'deleted'` and the same `meta`; when every retrieve answers 404 only that one is called. A retrieve that fails with another error is passed to `onError` and the event ends in `onResync` instead of `onChange`. Default `false` |
| `onError` | Called with the error of a refused `subscribe` or `poll` of the topic, and of a failed `fetch` retrieve |
| `topic` | The client topic to subscribe: the BE path relative to the application's `root_path`, without a Starlette `Mount` prefix. Default the ViewSet's `topic` option, else its `basePath` |

`onChange` and `onResync` are called in the order their events arrived, also while `fetch` waits for
a retrieve. With `fetch`, `onChange` is called for an event whose changes were all dropped, with an
empty `changes`. After `unsubscribe()` of the returned subscription no option is called, also for an
event that arrived before it and whose retrieve is still in flight.

The callbacks are function-typed properties: a callback whose parameter is typed other than the
option's, a wider record type included, does not compile. An `onChange` that declares only the
change parameter compiles.

| Throws `Error` | When |
|----------------|------|
| ``[ViewSet {basePath}] watch() needs an event client: pass `events` in the ViewSet's options`` | A REST ViewSet without `events` |
| `[ViewSet {basePath}] watch() found no event client for the peer: createEventClient({ peer }) registers one` | A muxws ViewSet without `events` whose peer has no client. Before a function `peer` has resolved, `watch()` returns, and once the peer resolves `ready` rejects with the error and `onError` is called with it; without `onError` it is rethrown in a microtask |

`RestProxy<M>` and `MuxwsProxy<M>` carry `watch()` as `ViewSetWatch<RecordOf<M>, KeyOf<M>>`, a
factory-built ViewSet as `ViewSetWatch<T, PkType<T, PK>>` ([`PkType`](./viewset-factory#signatures)).
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
/** A failed request's body naming one failure; `detail_*` appear with `df_viewset_exception_handler`. */
interface ApiErrorBody {
  detail: string;
  detail_code?: string;
  detail_params?: Record<string, unknown>;
}

/** One failure of a 422 body, as FastAPI reports it. */
interface FieldErrorEntry {
  type: string;
  loc: (string | number)[];
  msg: string;
  input?: unknown;
  ctx?: Record<string, unknown>;
}

interface FieldErrorsBody {
  detail: FieldErrorEntry[];
}

/** Assignable to vue-forms' `ErrorDescription`. */
interface ErrorDescription {
  readonly code: string;
  readonly params: Readonly<Record<string, unknown>>;
  readonly detail: string;
  readonly origin: 'server';
}

function toErrorDescription(body: ApiErrorBody): ErrorDescription;
function toFieldErrors(body: FieldErrorsBody): Record<string, ErrorDescription[]>;
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

### ListMixin `<T>`

```ts
list(params?: ListParams): ViewSetActionCall<T[]>;
```

### PaginatedListMixin `<T>`

```ts
listPage(params?: PageParams): ViewSetActionCall<PaginatedList<T>>;
```

### CursorListMixin `<T>`

```ts
listCursor(params?: CursorParams): ViewSetActionCall<CursorPage<T>>;
```

The three methods read the same `GET {basePath}`. Each sends `X-List-Shape` with its own shape - `plain`, `paginated`,
`cursor` - and throws `ListShapeMismatchError` when the reply is in another one. A BE viewset that declares one shape
does not read the header and answers in that shape, so a ViewSet declares the mixins of the shapes the BE viewset offers
through `list_shape` and `list_shapes`. A shape the BE viewset's `list_shapes` leave out is a 422
`fastapi-viewsets.unsupported_list_shape`.

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

### LookupMixin

```ts
lookup(): ViewSetActionCall<LookupItem[]>;
```

## Combined viewset mixins

A composite restates no signature: its interface extends the leaves its `actions` names, so each action is written in
exactly one place.

### ReadOnlyViewSetMixin `<K extends KeyType, T>`

Interface extends `ListMixin<T>` and `RetrieveMixin<K, T>`; `actions` spreads both.

### ViewSetMixin `<K extends KeyType, T, PK extends keyof T>`

Interface extends `ReadOnlyViewSetMixin<K, T>`, `CreateMixin<T, PK>`, `UpdateMixin<K, T>` and `DestroyMixin<K>`;
`actions` spreads all four.

### BulkViewSetMixin `<K extends KeyType, T, PK extends keyof T>`

Interface extends `ViewSetMixin<K, T, PK>`, `BulkOnlyCreateMixin<T, PK>`, `BulkOnlyUpdateMixin<K, T>` and
`BulkOnlyDestroyMixin<K>`; `actions` spreads all four.
