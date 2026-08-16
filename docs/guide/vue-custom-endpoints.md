# Custom Endpoints (Vue / TypeScript)

The standard actions (`list`, `retrieve`, `create`, …) map to fixed paths and verbs. A BE viewset
that serves more than those — `GET /items/search`, `POST /items/clone` — is matched on the FE by a
method in the ViewSet's own class body, written against `this.request()`.

## A method in the class body

```ts
import { restViewSet, LookupMixin, ReadOnlyViewSetMixin } from '@dynamicforms/fastapi-viewsets';

interface Item { id: number; name: string; description: string | null }

interface CloneRequest { source_id: number; new_name: string }

class ItemApi extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin, LookupMixin]) {
  /** Search items by name fragment. */
  async search(q: string): Promise<Item[]> {
    return this.request<Item[]>('GET', '/search', { query: { q } });
  }

  /** Clone an item under a new name. */
  async clone(body: CloneRequest): Promise<Item> {
    return this.request<Item>('POST', '/clone', { body });
  }
}

const itemsApi = new ItemApi({ basePath: '/items' });

// Declared actions and custom methods, on the same object:
const all = await itemsApi.list();
const one = await itemsApi.retrieve(1);
const results = await itemsApi.search('widget');
const cloned = await itemsApi.clone({ source_id: 1, new_name: 'Widget copy' });

// TS2339: create was not declared
await itemsApi.create({ name: 'Widget', description: null });
```

Four things about the base-class expression, each of which the compiler enforces:

- The empty `()` in `restViewSet<Item>()` is required. TypeScript has no partial type-argument
  inference, so the model cannot be given explicitly while the pk field and the mixin list are
  inferred from arguments in the same call (TS2558). Currying is the only way to have both.
- `'id'` is an argument, not a type argument. It is checked against the model's fields — a field
  that does not exist, or whose type cannot be a key, is TS2345 — and the pk type every action takes
  is read off `Item['id']`. It is not repeated in the constructor options.
- The mixin list decides the public surface. Calling an action it does not name is a compile error,
  not a 404 at run time.
- Constructor options are `{ basePath, validateSchema?, axiosInstance? }` for `restViewSet` and
  `{ basePath, validateSchema?, peer, headers?, timeoutMs? }` for `muxwsViewSet`. `pkFieldName` and
  `declares` are bound by the factory; passing either is TS2353.

The method's name is also what the [schema check](./route-rest#schema-validation) looks for: it
compares the BE's custom paths against methods of the same name, so `GET /items/search` wants a
`search()` and warns when the ViewSet has none.

See [Mixins](./vue-mixins) for the mixin list itself.

## this.request()

`request(method, path, options?)` sends one call and returns the decoded body. It is the only thing
a transport implements, which is why a method written against it works unchanged on REST and on
muxws.

| Argument | Value |
|----------|-------|
| `method` | `'GET'` \| `'POST'` \| `'PUT'` \| `'PATCH'` \| `'DELETE'` |
| `path` | relative to `basePath`: `''` for the collection, `'/search'`, `` `/${pk}/history` `` |
| `options` | `{ query?, body? }`, both optional |

An array-valued query entry becomes a repeated key, which is how FastAPI binds a `list[str]`
parameter. A status of 400 or above throws on both transports, and what is thrown and what a handler
reads off it is in [handling a failed call](./vue-mixins#handling-a-failed-call).

## Accessing protected members

A factory-built ViewSet descends from `ViewSetInternals`, which holds what a ViewSet's own methods
need and nothing a caller does:

| Member | Type | Description |
|--------|------|-------------|
| `this.request(method, path, opts?)` | `Promise<R>` | Sends one call and returns the decoded body. Transport-independent. |
| `this.basePath` | `string` | The base path, trailing slash removed, e.g. `'/items'` |

Both are `protected`, so reaching them from outside the class body is TS2445.

`this.http` is **not** available on a factory-built class (TS2339): the factory types its result as
these internals plus the declared actions, and the axios instance belongs to the REST transport
alone. A method that genuinely needs axios — response headers, a streaming download, an abort signal
— must extend `RestProxyImpl` instead.

## One endpoint, both transports

When the same custom endpoint has to be on a REST ViewSet and a muxws one, put it in a
class-expression mixin. The two classes come from separate factory calls and so have no common
ancestor to hang a shared method on.

```ts
import {
  CursorListMixin,
  RetrieveMixin,
  ViewSetInternals,
  muxwsViewSet,
  restViewSet,
} from '@dynamicforms/fastapi-viewsets';

interface CountEndpoint {
  count(): Promise<number>;
}

/**
 * The return type is annotated on purpose: an inferred one names the anonymous class and leaks its
 * protected members into the emitted .d.ts (TS4094).
 */
function WithCount<TBase extends abstract new (...args: any[]) => ViewSetInternals>(
  Base: TBase,
): TBase & (abstract new (...args: any[]) => CountEndpoint) {
  abstract class Counting extends Base {
    /** The method name must match the BE path segment (GET /items/count → count). */
    async count(): Promise<number> {
      return this.request<number>('GET', '/count');
    }
  }
  return Counting;
}

const DECLARES = [CursorListMixin, RetrieveMixin];

export class ItemRestApi extends WithCount(restViewSet<Item>()('id', DECLARES)) {}
export class ItemMuxwsApi extends WithCount(muxwsViewSet<Item>()('id', DECLARES)) {}
```

`ViewSetInternals` is the right constraint because it is exactly what the shared method uses;
bounding `TBase` by a transport would tie the method to that transport. Both classes keep their
declared actions and their own constructor options — `new ItemRestApi({ basePath: '/items' })`,
`new ItemMuxwsApi({ basePath: '/items', peer })` — and the method is on the prototype, so the schema
check finds it.

The demo does this for all four of its ViewSets: `demo/frontend/src/viewsets.ts`.

## Reaching axios: extending RestProxyImpl

`RestProxyImpl` is the REST transport itself, and a subclass of it has `this.http`. The cost is that
the class is HTTP-only, and that its type is the whole proxy: every standard action is present
whether or not the BE serves it, so only the schema check — at run time — can disagree.

```ts
import axios from 'axios';
import { BulkViewSetMixin, LookupMixin, RestProxyImpl } from '@dynamicforms/fastapi-viewsets';

class ItemApi extends RestProxyImpl<number, Item, 'id'> {
  static declares = [BulkViewSetMixin, LookupMixin];

  /** Needs axios itself: request() decodes the body and drops everything around it. */
  async exportCsv(): Promise<Blob> {
    const res = await this.http.get<Blob>(`${this.basePath}/export`, { responseType: 'blob' });
    return res.data;
  }
}

const http = axios.create({
  baseURL: 'https://api.example.com',
  headers: { Authorization: 'Bearer my-token' },
});

const itemsApi = new ItemApi({ basePath: '/items', pkFieldName: 'id', axiosInstance: http });
```

Here `pkFieldName` is a constructor option and `declares` a static, since there is no factory call
to bind them. A custom axios instance is passed the same way to a factory-built REST ViewSet:
`new ItemApi({ basePath: '/items', axiosInstance: http })`.

## The older form: route_rest

`route_rest` and `route_muxws` are unchanged and still supported. They build a bare proxy and cast
it to the mixin interface named as `M`:

```ts
import { route_rest, BulkViewSetMixin } from '@dynamicforms/fastapi-viewsets';

/** A type token: route_rest uses it for nothing but its type. */
class ItemTypeToken extends BulkViewSetMixin<number, Item, 'id'> {}

interface ItemApiInterface extends BulkViewSetMixin<number, Item, 'id'> {
  search(q: string): Promise<Item[]>;
}

const itemsApi = route_rest<ItemApiInterface>(ItemTypeToken, '/items', 'id');

await itemsApi.list();              // works
await itemsApi.search('widget');    // compiles, and throws: search is undefined
```

The cast is the limitation. The object is a `RestProxyImpl`, so the standard actions are there but a
custom endpoint named in `M` exists only in the type. A custom endpoint needs a class of your own:
the factory above, or `RestProxyImpl` extended directly.
