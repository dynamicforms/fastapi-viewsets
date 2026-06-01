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
import { route_rest, BulkViewSetMixin } from '@dynamicforms/viewsets';

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
import { route_rest, BulkViewSetMixin, LookupMixin } from '@dynamicforms/viewsets';

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
import { RestProxyImpl } from '@dynamicforms/viewsets';
import type { BulkViewSetMixin, LookupMixin } from '@dynamicforms/viewsets';

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

When a `RestProxyImpl` instance is created, it automatically fetches `GET {basePath}/schema` in the background and 
compares the BE's OpenAPI schema against the FE method set. If mismatches are found, a `console.warn` is emitted listing 
each discrepancy.

This check is **non-critical**: if the schema endpoint is unreachable (e.g. in unit tests) or returns an unexpected 
format, the error is silently ignored and no warning is shown.

### What gets checked

| Situation | Warning message |
|-----------|----------------|
| FE declares a standard method (e.g. `create`) but BE has no matching endpoint | `FE declares 'create()' but BE has no matching endpoint` |
| BE has a standard endpoint but FE does not implement the corresponding method | `BE exposes 'create' endpoint but FE does not implement it` |
| BE exposes a non-standard endpoint (e.g. `GET /items/export`) with no FE method | `BE has non-standard endpoint 'GET /items/export' with no FE method` |

### Example output

```
[ViewSet /items] FE/BE definition mismatch:
  • FE declares 'create()' but BE has no matching endpoint
  • FE declares 'update()' but BE has no matching endpoint
  • FE declares 'partialUpdate()' but BE has no matching endpoint
  • FE declares 'destroy()' but BE has no matching endpoint
  • BE has non-standard endpoint 'GET /items/export' with no FE method
```

This example would appear when the BE only exposes a read-only ViewSet (list + retrieve) plus a custom export endpoint, but the FE proxy was created with `RestProxyImpl` (which includes all CRUD methods).
