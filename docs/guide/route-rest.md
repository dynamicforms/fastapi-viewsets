# route_rest factory

`route_rest` is the frontend counterpart of the `route_viewset` decorator. It creates a fully typed HTTP client (backed by axios) that mirrors the operations of your backend viewset.

## Signature

```ts
function route_rest<M>(
  viewSetClass: ViewSetClass,
  basePath: string,
  pkFieldName: string,
  axiosInstance?: AxiosInstance,
): RestProxy<M>

// or with an options object:
function route_rest<M>(
  viewSetClass: ViewSetClass,
  options: RestProxyOptions,
): RestProxy<M>
```

## Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `viewSetClass` | class | The viewset class (used only as a type token; not instantiated) |
| `basePath` | `string` | Base URL path, e.g. `'/items'` |
| `pkFieldName` | `string` | Name of the PK field on the model, e.g. `'id'` |
| `axiosInstance` | `AxiosInstance` | Optional custom axios instance (defaults to global axios) |

## Basic usage

```ts
import { route_rest, BulkViewSetMixin } from '@dynamicforms/fastapi-viewsets';

interface Item {
  id: number;
  name: string;
  price: number;
}

class ItemViewSet extends BulkViewSetMixin<number, Item, 'id'> {}

const itemsApi = route_rest<BulkViewSetMixin<number, Item, 'id'>>(
  ItemViewSet,
  '/items',
  'id',
);
```

## Available methods

The returned proxy exposes all methods declared by the mixin type `M`:

```ts
// List all items
const items: Item[] = await itemsApi.list();

// Retrieve one item
const item: Item = await itemsApi.retrieve(1);

// Create (PK field is omitted from the payload)
const created: Item = await itemsApi.create({ name: 'Widget', price: 9.99 });

// Full update
const updated: Item = await itemsApi.update(1, { id: 1, name: 'Widget v2', price: 12.00 });

// Partial update
const patched: Item = await itemsApi.partialUpdate(1, { price: 11.00 });

// Delete
const result = await itemsApi.destroy(1);

// Bulk create
const createdMany: Item[] = await itemsApi.bulkCreate([
  { name: 'A', price: 1 },
  { name: 'B', price: 2 },
]);

// Bulk update
const updatedMany: Item[] = await itemsApi.bulkUpdate({ 1: { id: 1, name: 'A2', price: 1.5 } });

// Bulk partial update
const patchedMany: Item[] = await itemsApi.bulkPartialUpdate({ 1: { price: 2.0 } });

// Bulk delete
const deletedMany = await itemsApi.bulkDestroy([1, 2, 3]);

// Lookup (requires LookupMixin)
const options = await itemsApi.lookup();
```

## Using with LookupMixin

```ts
import { route_rest, BulkViewSetMixin, LookupMixin } from '@dynamicforms/fastapi-viewsets';

const itemsApi = route_rest<BulkViewSetMixin<number, Item, 'id'> & LookupMixin>(
  ItemViewSet,
  '/items',
  'id',
);

const lookupItems = await itemsApi.lookup();
// [{ group: null, pk: 1, title: 'Widget', icon: null }, ...]
```

## Using a custom axios instance

```ts
import axios from 'axios';

const http = axios.create({
  baseURL: 'https://api.example.com',
  headers: { Authorization: 'Bearer my-token' },
});

const itemsApi = route_rest<BulkViewSetMixin<number, Item, 'id'>>(
  ItemViewSet,
  '/items',
  'id',
  http,
);
```

Or with the options object form:

```ts
const itemsApi = route_rest<BulkViewSetMixin<number, Item, 'id'>>(
  ItemViewSet,
  { basePath: '/items', pkFieldName: 'id', axiosInstance: http },
);
```

## Direct instantiation via `RestProxyImpl`

Instead of using the `route_rest` factory, you can instantiate `RestProxyImpl` directly:

```ts
import { RestProxyImpl } from '@dynamicforms/fastapi-viewsets';
import type { BulkViewSetMixin, LookupMixin } from '@dynamicforms/fastapi-viewsets';

let proxy: BulkViewSetMixin<number, Item, 'id'> & LookupMixin;

proxy = new RestProxyImpl<number, Item, 'id'>({
  axiosInstance: http,
  basePath: '/items',
  pkFieldName: 'id',
});
```

`RestProxyImpl` accepts a single `RestProxyOptions` object:

| Option | Type | Description |
|--------|------|-------------|
| `basePath` | `string` | Base URL path, e.g. `'/items'` |
| `pkFieldName` | `string` | Name of the PK field on the model, e.g. `'id'` |
| `axiosInstance` | `AxiosInstance` | Optional custom axios instance (defaults to global axios) |

The difference between the two approaches:

| | `route_rest(...)` | `new RestProxyImpl(...)` |
|---|---|---|
| Typical use | Application code | Tests / advanced use |
| Type inference | Via generic `M` parameter | Via generic type parameters on class |
| ViewSet class argument | Required (type token) | Not needed |
| Result | `RestProxy<M>` (typed as `M`) | `RestProxyImpl` instance |

Both produce the same underlying object — `route_rest` simply wraps `new RestProxyImpl(options)`.

## HTTP mapping

| Method | HTTP call |
|--------|-----------|
| `list()` | `GET /items` |
| `retrieve(pk)` | `GET /items/{pk}` |
| `create(data)` | `POST /items` |
| `update(pk, data)` | `PUT /items/{pk}` |
| `partialUpdate(pk, data)` | `PATCH /items/{pk}` |
| `destroy(pk)` | `DELETE /items/{pk}` |
| `bulkCreate(data[])` | `POST /items/bulk` |
| `bulkUpdate(records)` | `PUT /items/bulk` |
| `bulkPartialUpdate(records)` | `PATCH /items/bulk` |
| `bulkDestroy(pks[])` | `DELETE /items/bulk` |
| `lookup()` | `GET /items/lookup` |

## Schema validation

A ViewSet lists the mixins it is made of, and the proxy checks that list against the BE's schema
when it is constructed:

```ts
import { BulkViewSetMixin, LookupMixin } from '@dynamicforms/fastapi-viewsets';

class ItemViewSet extends RestProxyImpl<number, Item, 'id'> {
  static declares = [BulkViewSetMixin, LookupMixin];
}
```

`declares` is the FE counterpart of a BE viewset's base classes, and it exists because a
TypeScript `implements` clause cannot do the job: it is erased before anything runs. On these
classes it does not even narrow the type — the proxy base implements every action, so any subset is
satisfied trivially — which is why `declares` replaces the `implements` clause rather than
repeating it.

The proxy fetches `GET {basePath}/schema` in the background and emits one `console.warn` listing
every disagreement. The check is **non-critical**: an unreachable schema endpoint or an unexpected
format is ignored silently.

### What gets checked

| Situation | Warning message |
|-----------|----------------|
| The ViewSet declares an action the BE viewset does not serve | `declares 'create' but the BE viewset serves no such endpoint` |
| The BE serves a standard endpoint the ViewSet never declared | `BE serves 'POST /items' but the ViewSet declares no create` |
| The BE serves a custom endpoint the ViewSet has no method for | `BE serves 'GET /items/export' but the ViewSet has no 'export()' method` |

`GET {basePath}` is one endpoint that answers in whichever shape the BE viewset declared, so
`ListMixin`, `PaginatedListMixin` and `CursorListMixin` each satisfy it — declaring `listCursor`
and being served `GET /items` is agreement, not a mismatch.

Custom endpoints are the one place the check asks whether a method exists on the object, because
there the answer means something: the base implements no custom actions, so whatever answers is
something the ViewSet's own author wrote.

### Example output

```
[ViewSet /items] FE/BE definition mismatch:
  • declares 'create' but the BE viewset serves no such endpoint
  • declares 'update' but the BE viewset serves no such endpoint
  • BE serves 'GET /items/export' but the ViewSet has no 'export()' method
```

### A ViewSet that declares nothing

It is not checked, and does not pay for the schema request. That is deliberate: a ViewSet that
never said what it has cannot be caught contradicting itself, and guessing on its behalf is what
made the check report actions nobody had claimed.

To narrow a declaration in a subclass, state the smaller list — ordinary static lookup means it
replaces the parent's rather than adding to it:

```ts
/** Same model, but this BE viewset was built from CursorListMixin alone. */
class ItemDbViewSet extends ItemViewSet {
  static declares = [CursorListMixin];
}
```

Set `validateSchema: false` to skip the check entirely.
