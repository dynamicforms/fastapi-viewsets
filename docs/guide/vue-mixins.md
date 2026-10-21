# Vue / TypeScript Mixins

The `@dynamicforms/fastapi-viewsets` package ships TypeScript mixin classes that mirror the Python
backend mixins. You list them in a ViewSet declaration to say which operations that viewset has; the
list types the class and is what the startup schema check compares against the BE. The HTTP calls
themselves live in the proxy underneath.

## Individual operation mixins

| Class | Methods declared |
|-------|-----------------|
| `CreateMixin<T, PK>` | `create(data)` |
| `BulkOnlyCreateMixin<T, PK>` | `bulkCreate(data[])` |
| `BulkCreateMixin<T, PK>` | `create`, `bulkCreate` |
| `ListMixin<T>` | `list(params?)` |
| `PaginatedListMixin<T>` | `listPage(params?)` |
| `CursorListMixin<T>` | `listCursor(params?)` |
| `RetrieveMixin<K, T>` | `retrieve(pk)` |
| `UpdateMixin<K, T>` | `update(pk, data)`, `partialUpdate(pk, data)` |
| `BulkOnlyUpdateMixin<K, T>` | `bulkUpdate(records)`, `bulkPartialUpdate(records)` |
| `BulkUpdateMixin<K, T>` | all four update methods |
| `DestroyMixin<K>` | `destroy(pk)` |
| `BulkOnlyDestroyMixin<K>` | `bulkDestroy(pks[])` |
| `BulkDestroyMixin<K>` | `destroy`, `bulkDestroy` |
| `LookupMixin<L = LookupItem>` | `lookup(params?)` |

## Combined viewset mixins

| Class | Extends |
|-------|---------|
| `ReadOnlyViewSetMixin<K, T>` | `ListMixin`, `RetrieveMixin` |
| `ViewSetMixin<K, T, PK>` | `ReadOnlyViewSetMixin` + `CreateMixin`, `UpdateMixin`, `DestroyMixin` |
| `BulkViewSetMixin<K, T, PK>` | `ViewSetMixin` + `BulkOnlyCreateMixin`, `BulkOnlyUpdateMixin`, `BulkOnlyDestroyMixin` |

## Type parameters

| Parameter | Description |
|-----------|-------------|
| `K` | Primary key type — `number` or `string` |
| `T` | Model interface |
| `PK` | Key of the PK field on `T` (e.g. `'id'`) — used to omit it from create payloads |

## LookupItem

`lookup(params?)` sends `params` as query parameters, the fields of the BE viewset's lookup filter
model (`q`, and e.g. `ids`; an array goes out as a repeated key). It returns `L[]`, where `L` is the
second type argument of `restViewSet<T, L>()` / `muxwsViewSet<T, L>()`, the BE viewset's
`TLookupItem`. The default is `LookupItem`:

```ts
interface LookupItem {
  group: unknown;
  pk: unknown;
  title: string;
  icon: string | null;
}
```

## Declaring a viewset class

A ViewSet is a class extending what `restViewSet` — or `muxwsViewSet`, for the same surface over a
muxws stream — hands back:

```ts
import {
  restViewSet, ReadOnlyViewSetMixin, LookupMixin, CursorListMixin,
} from '@dynamicforms/fastapi-viewsets';

interface Item {
  id: number;
  name: string;
  price: number;
}

class ItemApi extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin, LookupMixin]) {
  /** GET /items/count */
  async count(): Promise<number> {
    return this.request<number>('GET', '/count');
  }
}

const api = new ItemApi({ basePath: '/items' });

await api.list();
await api.count();
await api.create({ name: 'Widget', price: 9.99 });  // TS2339: create was not declared
```

The mixin list is written once, as values, and does three jobs: it decides which actions the class
exposes to callers, it types them, and it is handed to the proxy so that the startup
[schema check](./route-rest#schema-validation) can compare it against the BE. Calling an action the
ViewSet did not declare is a compile error rather than a 404 at runtime.

The empty `()` is required. TypeScript has no partial type-argument inference, so the model cannot be
given explicitly while the pk field and the mixin list are inferred from arguments in the same call
(TS2558); currying is the only way to have both.

### The primary key

`'id'` is an argument, not a type argument. It is checked against the model's fields — a name that is
not a field of `Item`, or a field that cannot be a key, is TS2345 — and the pk *type* is read off
`Item['id']`, so `api.retrieve('1')` does not compile against the model above. It is not repeated in
the constructor options.

### Constructor options

| Option | Factory | Description |
|--------|---------|-------------|
| `basePath` | both | Base path to the resource, e.g. `'/items'` |
| `validateSchema` | both | Set `false` to skip the startup schema check |
| `repeat` | both | [`RepeatOptions`](../api/idempotency#repeatoptions) of a call whose attempt ended without an answer; default `{ withinMs: 600000, delayMs: 1000 }` |
| `events` | both | The event client [`watch()`](#following-changes-watch) subscribes on; without it, a `muxwsViewSet` uses the client registered for its peer and a `restViewSet` throws from `watch()` |
| `topic` | both | The client topic `watch()` subscribes: the BE path relative to the app's `root_path`, without a Starlette `Mount` prefix; default `basePath` |
| `axiosInstance` | `restViewSet` | Custom axios instance; defaults to the global one |
| `attemptTimeoutMs` | `restViewSet` | Timeout of one attempt in milliseconds; default `30000`, `0` for none |
| `peer` | `muxwsViewSet` | The muxws peer, or a function returning one (required) |
| `headers` | `muxwsViewSet` | Sent on every command, on top of the handshake's |
| `timeoutMs` | `muxwsViewSet` | Per-attempt timeout, from opening the stream to the last byte of the reply; on expiry the call is repeated |

`pkFieldName` and `declares` are bound by the factory and are TS2353 if passed.

### Inside the class body

A ViewSet's own methods reach `this.request(method, path, options?)` and `this.basePath`. Both are
`protected`, so a caller touching them is TS2445. `this.http` is not among them — a method that needs
axios directly extends [`RestProxyImpl`](./vue-custom-endpoints) instead, at the cost of working on
one transport only.

### Following changes: watch()

Every ViewSet has `watch(options)` besides its declared actions, whichever mixins it declares. It
subscribes the ViewSet's topic (`options.topic`, else the `topic` constructor option, else
`basePath`) to the backend's `records_changed` event and calls `onChange` once per create, update or
destroy action on that viewset:

```ts
const watching = api.watch({
  fetch: true,
  onChange(change) { rows.value = applyChanges(rows.value, change, api.pkFieldName); },
  onResync() { void reload(); },
});
onUnmounted(() => watching.unsubscribe());
```

A `restViewSet` needs an event client in its options, `new ItemApi({ basePath: '/items', events })`;
a `muxwsViewSet` uses the one created for its peer. The event client, the options and complete
grid examples are in [Events (Vue)](./vue-events#watch-on-a-viewset); the reference is
[`watch()`](../api/vue-mixins#watch).

### Overriding a declared action

A declared action is an ordinary method and can be overridden, `super` included:

```ts
class Cached extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin]) {
  override async list(): Promise<Item[]> {
    return super.list();
  }
}
```

### Narrowing

To point the same model at a BE viewset that serves less, call the factory again with the smaller
list:

```ts
/** Same model, but this BE viewset was built from CursorListMixin alone. */
class ItemDbApi extends restViewSet<Item>()('id', [CursorListMixin]) {}
```

Subclassing the wider ViewSet and restating `static declares` does not work: a factory-built class
pins the list to the tuple the factory was given, and the subclass is TS2417. The separate class's
type has `listCursor` and nothing else.

### route_rest

[`route_rest`](./route-rest) and `route_muxws` are the other form. They return a bare
proxy cast to the mixin interface named in `M`: the standard actions are there, but a custom endpoint
named in that interface type-checks and is `undefined` when called, because the object is not the
consumer's own class.

## Handling a failed call

A status of 400 or above rejects on both transports, and which class is thrown depends on which one
is in use:

| Transport | Thrown |
|-----------|--------|
| `restViewSet`, `route_rest` | axios' own `AxiosError` |
| `muxwsViewSet`, `route_muxws` | `ViewSetRequestError`, exported by the package |

Below 400 the two disagree: axios rejects on any status outside 200-299 — its default
`validateStatus`, which this package never sets and an `axiosInstance` of your own may — while the
muxws proxy throws only at 400 and above. Most 3xx replies never reach a REST caller, because the
transport follows the redirect itself; one it does not follow — a 304, or a redirect with no
`Location` — is what actually rejects there, while muxws returns any 3xx as a body.

Both classes carry `error.response.status`, `error.response.data` and `error.response.headers`, so a
handler reading those three reads the same fields whichever was thrown. `data` is the decoded
response body, the BE's `ApiErrorBody` for every error of a viewset (see
[Error codes](./error-codes#the-body-of-a-failed-call), and `errorBodyOf(error)` for a call without a response).

They differ in whether `response` is there at all: `ViewSetRequestError` always sets it, while
`AxiosError.response` is `undefined` when the request never got a reply. A handler that also has to
cover a connection failure therefore checks it before reading a status:

```ts
async function retrieveOrNull(id: number): Promise<Item | null> {
  try {
    return await api.retrieve(id);
  } catch (error) {
    const response = (error as { response?: { status: number; data: unknown } }).response;
    if (response?.status === 404) return null;
    throw error;
  }
}
```

A muxws call whose socket dies mid-flight is repeated once the peer reconnects (see
[Idempotent Calls](./idempotency#frontend)). Once `repeat.withinMs` has passed, or the peer has
closed for good, it rejects with muxws' `ConnectionLost`, not with `ViewSetRequestError`: it carries
no `response`, so the check above sends it down the same branch as an `AxiosError` that never got a
reply. See [reconnection](./muxws#reconnection).

`list()`, `listPage()` and `listCursor()` on either transport reject with `ListShapeMismatchError`
when a successful reply is in another list shape than the one the method requested. It carries no
`response` either; `expected` and `received` name the two shapes. See
[the list mixins](../api/vue-mixins#cursorlistmixin-t).
