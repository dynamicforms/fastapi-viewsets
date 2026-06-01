# Custom Endpoints (Vue / TypeScript)

`RestProxyImpl` (and `route_rest`) map fixed method names (`list`, `retrieve`, `create`, …) to HTTP calls. For a custom endpoint that doesn't match any of those, extend `RestProxyImpl` and add the method yourself.

---

## Extending RestProxyImpl

```ts
import axios from 'axios';
import { RestProxyImpl } from '@dynamicforms/viewsets';
import type { BulkViewSetMixin } from '@dynamicforms/viewsets';

interface Item { id: number; name: string; description: string | null }

interface CloneRequest { source_id: number; new_name: string }

class ItemApi extends RestProxyImpl<number, Item, 'id'> {
  /** Search items by name fragment. */
  async search(q: string): Promise<Item[]> {
    const res = await this.http.get<Item[]>(`${this.basePath}/search`, { params: { q } });
    return res.data;
  }

  /** Clone an item under a new name. */
  async clone(body: CloneRequest): Promise<Item> {
    const res = await this.http.post<Item>(`${this.basePath}/clone`, body);
    return res.data;
  }
}

// Instantiate directly (no route_rest needed for subclasses):
const itemsApi = new ItemApi({ basePath: '/items', pkFieldName: 'id' });

// Standard mixin methods still work:
const all = await itemsApi.list();
const one = await itemsApi.retrieve(1);

// Custom methods:
const results  = await itemsApi.search('widget');
const cloned   = await itemsApi.clone({ source_id: 1, new_name: 'Widget copy' });
```

## Accessing protected members

`RestProxyImpl` exposes two `protected` members you can use inside subclass methods:

| Member | Type | Description |
|--------|------|-------------|
| `this.http` | `AxiosInstance` | The axios instance (custom or global) |
| `this.basePath` | `string` | The base path, e.g. `'/items'` |

## Using a custom axios instance

```ts
const http = axios.create({
  baseURL: 'https://api.example.com',
  headers: { Authorization: 'Bearer my-token' },
});

const itemsApi = new ItemApi({ basePath: '/items', pkFieldName: 'id', axiosInstance: http });
```

## Type-safe declaration

If you want the extended API to be typed as a union of the mixin interface and your custom methods, declare an interface:

```ts
interface ItemApiInterface extends BulkViewSetMixin<number, Item, 'id'> {
  search(q: string): Promise<Item[]>;
  clone(body: CloneRequest): Promise<Item>;
}

const itemsApi: ItemApiInterface = new ItemApi({ basePath: '/items', pkFieldName: 'id' });
```
