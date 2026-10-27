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
): ViewSetCalls<M>

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
  /** When a call whose attempt ended without an answer is repeated. */
  repeat?: RepeatOptions;
  /** The event client watch() subscribes on; without it, watch() throws. */
  events?: EventClient;
  /** The client topic watch() subscribes; default basePath. */
  topic?: string;
  /** Optional: existing axios instance. Defaults to the global axios. */
  axiosInstance?: AxiosInstance;
  /** The timeout of one attempt in milliseconds, passed to axios as `timeout`; 0 for none. */
  attemptTimeoutMs?: number;
}
```

## Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `viewSetClass` | `abstract new (...args) => any` | Viewset class — never instantiated. Only its `static declares` is read, and handed to the proxy for the schema check. |
| `basePath` | `string` | Base URL path, e.g. `'/items'`. Trailing slash is stripped automatically. |
| `pkFieldName` | `string` | Name of the PK field on the model |
| `axiosInstance` | `AxiosInstance` | Optional custom axios instance; defaults to global `axios` |
| `attemptTimeoutMs` | `number` | Options object only. Timeout of one attempt in milliseconds, default `30000`, `0` for none |
| `repeat` | [`RepeatOptions`](./idempotency#repeatoptions) | Options object only. Default `{ withinMs: 600000, delayMs: 1000 }` |
| `events` | `EventClient` | Options object only. The event client [`watch()`](./vue-mixins#watch) subscribes on; without it, `watch()` throws |
| `topic` | `string` | Options object only. The client topic `watch()` subscribes: the BE path relative to the application's `root_path`, without a Starlette `Mount` prefix. Default `basePath` |

## Return value

The options form returns a `RestProxy<M>`, which is `ViewSetCalls<M>` — `M` with every method
returning a [`ViewSetCall`](./idempotency#frontend) — and [`watch()`](./vue-mixins#watch), typed as
`ViewSetWatch<RecordOf<M>, KeyOf<M>>`: the record and pk types read off `M`'s actions. The
positional form takes no `events` and returns `ViewSetCalls<M>`, without `watch()`. Either is an
instance of `RestProxyImpl` cast to it. The cast is unchecked: `M` is
whatever the caller wrote, and nothing compares it with the object. The standard actions listed under [HTTP
mapping](#http-mapping) are implemented by `RestProxyImpl` and are therefore really there; anything else named in `M` —
a custom endpoint of your own — type-checks at the call site and is `undefined` when called.

The object is a bare `RestProxyImpl`, not an instance of `viewSetClass`: methods written on that class do not survive
the cast. To have a ViewSet's own methods exist on the object, subclass `RestProxyImpl` as below, or build the class
with the `restViewSet` factory.

Passing a factory-built class to `route_rest` fails to compile — it would otherwise type-check and hand back an object
without that class's own methods. A factory-built ViewSet is constructed directly:
`new ItemApi({ basePath: '/items' })`.

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

Each attempt of a call goes through the axios instance's per-verb method (`get`, `post`, `put`, `patch`, `delete`) with
a config of the call's `params` and `headers`, `Idempotency-Key`, `timeout: attemptTimeoutMs` and the attempt's
`signal`. An attempt that ends in an axios error without `response` (other than a cancellation), or with `502`, `503`
or `504` whose body is not a JSON object with `detail`, is repeated as described in
[Idempotent Calls](../guide/idempotency#repeats); its signal is aborted first. A repeat is sent with the headers of the
first attempt's `config` after the request interceptors, other than `Accept`, `Accept-Encoding`, `Content-Length`,
`Content-Type` and `User-Agent`, under the call's own headers.

## HTTP mapping

| Method | HTTP verb | URL |
|--------|-----------|-----|
| `list()` | `GET` | `{basePath}` (header `X-List-Shape: plain`) |
| `listPage(params)` | `GET` | `{basePath}` (query: `offset`, `limit`; header `X-List-Shape: paginated`) |
| `listCursor(params)` | `GET` | `{basePath}` (query: `cursor`, `limit`; header `X-List-Shape: cursor`) |
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

`watch(options)` sends no request to `{basePath}`: it subscribes the topic `options.topic`, else
the `topic` option, else `basePath`, on the ViewSet's event client; see
[`watch()`](./vue-mixins#watch).
