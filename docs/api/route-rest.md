# route_rest — API Reference

```ts
import { route_rest } from '@dynamicforms/viewsets';
```

## Signatures

```ts
function route_rest<M>(
  viewSetClass: ViewSetClass,
  basePath: string,
  pkFieldName: string,
  axiosInstance?: AxiosInstance,
): RestProxy<M>

function route_rest<M>(
  viewSetClass: ViewSetClass,
  options: RestProxyOptions,
): RestProxy<M>
```

## RestProxyOptions

```ts
interface RestProxyOptions {
  basePath: string;
  pkFieldName: string;
  axiosInstance?: AxiosInstance;
}
```

## Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `viewSetClass` | `abstract new (...args) => any` | Viewset class — used as a type token only, never instantiated |
| `basePath` | `string` | Base URL path, e.g. `'/items'`. Trailing slash is stripped automatically. |
| `pkFieldName` | `string` | Name of the PK field on the model |
| `axiosInstance` | `AxiosInstance` | Optional custom axios instance; defaults to global `axios` |

## Return value

Returns a `RestProxy<M>` — an instance of `RestProxyImpl` cast to the mixin interface `M`. All methods declared by `M` are available and fully typed.

## RestProxyImpl

The concrete class behind every proxy. Can be subclassed for custom behaviour:

```ts
import { RestProxyImpl } from '@dynamicforms/viewsets';

class AuthenticatedProxy<K, T, PK extends keyof T> extends RestProxyImpl<K, T, PK> {
  async list(): Promise<T[]> {
    // custom pre/post processing
    return super.list();
  }
}
```

## HTTP mapping

| Method | HTTP verb | URL |
|--------|-----------|-----|
| `list()` | `GET` | `{basePath}` |
| `retrieve(pk)` | `GET` | `{basePath}/{pk}` |
| `create(data)` | `POST` | `{basePath}` |
| `update(pk, data)` | `PUT` | `{basePath}/{pk}` |
| `partialUpdate(pk, data)` | `PATCH` | `{basePath}/{pk}` |
| `destroy(pk)` | `DELETE` | `{basePath}/{pk}` |
| `bulkCreate(data[])` | `POST` | `{basePath}/bulk` |
| `bulkUpdate(records)` | `PUT` | `{basePath}/bulk` |
| `bulkPartialUpdate(records)` | `PATCH` | `{basePath}/bulk` |
| `bulkDestroy(pks[])` | `DELETE` | `{basePath}/bulk` (body: pk array) |
| `lookup()` | `GET` | `{basePath}/lookup` |
