# route_rest — API Reference

```ts
import { route_rest } from '@dynamicforms/fastapi-viewsets';
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
  /** Set false to skip the startup schema check (advisory, costs one request). */
  validateSchema?: boolean;
  /** The mixins the ViewSet declares, for the schema check. */
  declares?: readonly ViewSetMixinDeclaration[];
  /** Optional: existing axios instance. Defaults to the global axios. */
  axiosInstance?: AxiosInstance;
}
```

## Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `viewSetClass` | `abstract new (...args) => any` | Viewset class — never instantiated. Only its `static declares` is read, and handed to the proxy for the schema check. |
| `basePath` | `string` | Base URL path, e.g. `'/items'`. Trailing slash is stripped automatically. |
| `pkFieldName` | `string` | Name of the PK field on the model |
| `axiosInstance` | `AxiosInstance` | Optional custom axios instance; defaults to global `axios` |

## Return value

Returns a `RestProxy<M>`, which is `M` — an instance of `RestProxyImpl` cast to it. The cast is unchecked: `M` is whatever the caller wrote, and nothing compares it with the object. The standard actions listed under [HTTP mapping](#http-mapping) are implemented by `RestProxyImpl` and are therefore really there; anything else named in `M` — a custom endpoint of your own — type-checks at the call site and is `undefined` when called.

The object is a bare `RestProxyImpl`, not an instance of `viewSetClass`: methods written on that class do not survive the cast. To have a ViewSet's own methods exist on the object, subclass `RestProxyImpl` as below, or build the class with the `restViewSet` factory.

Do not pass a factory-built class to `route_rest`. It type-checks and hands back an object without that class's own methods. A factory-built ViewSet is constructed directly: `new ItemApi({ basePath: '/items' })`.

## RestProxyImpl

The concrete class behind every proxy. Can be subclassed for custom behaviour:

```ts
import { RestProxyImpl, type KeyType } from '@dynamicforms/fastapi-viewsets';

class AuthenticatedProxy<K extends KeyType, T, PK extends keyof T> extends RestProxyImpl<K, T, PK> {
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
