# restViewSet / muxwsViewSet — API Reference

```ts
import { restViewSet, muxwsViewSet } from '@dynamicforms/fastapi-viewsets';
```

The ViewSet class factory — the FE counterpart of listing mixins in a BE viewset's bases. It hands back a class to extend:

```ts
import {
  restViewSet, ReadOnlyViewSetMixin, LookupMixin, CursorListMixin, RetrieveMixin,
} from '@dynamicforms/fastapi-viewsets';

interface Item { id: number; name: string }

class ItemApi extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin, LookupMixin]) {
  async count(): Promise<number> {
    return this.request<number>('GET', '/count');
  }
}

const api = new ItemApi({ basePath: '/items' });
await api.list();
await api.count();
await api.create({ name: 'x' }); // TS2339: create was not declared
```

The mixin list is written once, as values, and does three jobs: it decides which actions the class exposes to callers, it types them, and it is handed to the proxy so the startup schema check can compare it against the BE.

## Signatures

```ts
function restViewSet<T>(): <
  PK extends PkFieldName<T> & keyof T,
  D extends readonly ViewSetMixinClass[],
>(pkFieldName: PK, declares: D) => ViewSetClass<T, PK, D, RestProxyOptions>

function muxwsViewSet<T>(): <
  PK extends PkFieldName<T> & keyof T,
  D extends readonly ViewSetMixinClass[],
>(pkFieldName: PK, declares: D) => ViewSetClass<T, PK, D, MuxwsProxyOptions>
```

Supporting types:

```ts
/** A mixin class: the runtime `actions` the schema check reads, and the type naming those actions. */
type ViewSetMixinClass = ViewSetMixinDeclaration & (abstract new (...args: any[]) => object);

/** The fields of `T` that could be a primary key — those whose type is string or number. */
type PkFieldName<T> = Extract<
  { [F in keyof T]-?: NonNullable<T[F]> extends KeyType ? F : never }[keyof T],
  string
>;

type ViewSetClass<T, PK extends keyof T, D extends readonly ViewSetMixinClass[], O extends ProxyBaseOptions> =
  // A declaration naming no action resolves to its own error message; see TS2507 below.
  [ActionsOf<D[number]>] extends [never]
    ? 'declares must name at least one action: pass the mixin classes themselves, unannotated'
    : {
        new (options: Omit<O, 'pkFieldName' | 'declares'>): ViewSetInternals &
          ActionSurface<PkType<T, PK>, T, PK, ActionsOf<D[number]>>;
        readonly declares: D;
      };
```

## Why the call is curried

The empty `()` in `restViewSet<Item>()('id', [...])` is required. TypeScript has no partial type-argument inference: in a single call, the model cannot be given explicitly while the pk field and the mixin list are inferred from arguments. Naming only `T` on a three-parameter signature is `TS2558: Expected 3 type arguments, but got 1`. Two calls is the only way to have both.

## Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `T` (type argument) | — | The record model, e.g. `Item`. Explicit, on the first call. |
| `pkFieldName` | `PkFieldName<T> & keyof T` | Name of the PK field on the model, e.g. `'id'`. An argument, not a type argument: it is checked against the model's fields, and the pk *type* is read off `T[PK]`. |
| `declares` | `readonly ViewSetMixinClass[]` | The mixin classes themselves, as values. |

`retrieve`, `update`, `destroy` and the bulk actions take a pk of the type the model gives it — with `Item['id']: number`, `api.retrieve('1')` is `TS2345: Argument of type 'string' is not assignable to parameter of type 'number'`.

## Constructor options

The returned class binds `pkFieldName` and `declares` itself, so its constructor takes what is left:

```ts
// restViewSet
interface Options {
  basePath: string;
  validateSchema?: boolean;
  axiosInstance?: AxiosInstance;
}

// muxwsViewSet
interface Options {
  basePath: string;
  validateSchema?: boolean;
  peer: MuxwsPeerSource;
  timeoutMs?: number;
  headers?: Record<string, string>;
}
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `basePath` | `string` | — | Base path to the resource, e.g. `'/items'`. A trailing slash is stripped. |
| `validateSchema` | `boolean` | `true` | Set `false` to skip the startup schema check (it is advisory and costs one request). |
| `axiosInstance` | `AxiosInstance` | global `axios` | REST only. |
| `peer` | `MuxwsPeerSource` | — | muxws only. A `Peer`, or a function returning one — called once and cached. |
| `timeoutMs` | `number` | — | muxws only. Per-request timeout; muxws resets the stream with `TIMEOUT` when it expires. |
| `headers` | `Record<string, string>` | `{}` | muxws only. Headers added to every request. |

## Inside the class body

A ViewSet's own methods reach `this.request()` and `this.basePath`, inherited from `ViewSetInternals`:

```ts
protected readonly basePath: string;
protected request<R>(method: HttpMethod, path: string, options?: RequestOptions): Promise<R>;
```

`path` is relative to `basePath` — `''` for the collection, `/1` for a record. Writing a custom endpoint against `request()` rather than against a transport is what lets the same method body serve a REST and a muxws ViewSet.

Both members are `protected`, so a caller cannot touch them: `api.basePath` outside the class is `TS2445: Property 'basePath' is protected and only accessible within class 'ViewSetInternals' and its subclasses`.

`this.http` is **not** available on a factory-built class — the factory types its result as `ViewSetInternals` plus the declared actions, and the axios instance is not part of that. A method that needs axios directly extends `RestProxyImpl` instead:

```ts
import { RestProxyImpl } from '@dynamicforms/fastapi-viewsets';

class DirectAxios extends RestProxyImpl<number, Item, 'id'> {
  async ping(): Promise<void> {
    await this.http.get('/ping');
  }
}
```

## Overriding a declared action

A declared action can be overridden with a method, `super` included:

```ts
class Cached extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin]) {
  override async list(): Promise<Item[]> {
    return super.list();
  }
}
```

## Narrowing a ViewSet

A factory-built class pins `declares` to the exact tuple it was given, so restating it on a subclass does not compile:

```ts
class Narrowed extends ItemApi {
  static declares = [CursorListMixin];   // TS2417
}
```

To point a ViewSet at a smaller BE viewset, call the factory again with the smaller list:

```ts
class ItemDbApi extends restViewSet<Item>()('id', [CursorListMixin, RetrieveMixin]) {}
```

## Compile errors

| Code | Situation | Meaning |
|------|-----------|---------|
| `TS2339` | `api.create(...)` on a ViewSet that did not declare `create` | *Property 'create' does not exist on type 'ItemApi'.* The action is not in the declaration, so it is not on the type — caught at compile time rather than as a 404 at runtime. |
| `TS2339` | `this.http` in a factory-built class body | *Property 'http' does not exist on type 'ItemApi'.* Extend `RestProxyImpl` for direct axios access. |
| `TS2345` | `restViewSet<Item>()('nope', [...])` | *Argument of type '"nope"' is not assignable to parameter of type '"name" \| "id"'.* The pk field must exist on the model and be a `string` or `number` field — a `string[]` field is rejected the same way. |
| `TS2353` | `new ItemApi({ basePath: '/items', pkFieldName: 'id' })` | *'pkFieldName' does not exist in type `Omit<RestProxyOptions, "pkFieldName" \| "declares">`.* Both are bound by the factory; passing them again is an error. The same applies to `declares`. |
| `TS2417` | `static declares = [...]` on a subclass of a factory-built class | *Class static side incorrectly extends base class static side.* `declares` is pinned to the tuple the factory was given. Call the factory again with the smaller list instead of restating it. |
| `TS2507` | `declares` names no action — `[]`, or an array annotated `ViewSetMixinClass[]`, which erases which mixins are in it | *Type `'declares must name at least one action: pass the mixin classes themselves, unannotated'` is not a constructor function type.* The message is the return type: a ViewSet with no actions would otherwise be built without complaint. |
| `TS2558` | `restViewSet<Item, 'id', [...]>(...)` | *Expected 1 type arguments, but got 3.* The call is curried; see [above](#why-the-call-is-curried). |

## Schema check

On construction, unless `validateSchema: false`, the ViewSet fetches `GET {basePath}/schema` over its own transport and compares the declaration against it, `console.warn`-ing any disagreement: an action declared that the BE does not serve, an endpoint served that the ViewSet declares no action for, and a custom BE endpoint the ViewSet has no method of that name for. It is advisory — failures to fetch or parse are ignored.

## Relation to route_rest / route_muxws

[`route_rest`](/api/route-rest) and `route_muxws` are unchanged and still supported; they are the older form. They return a bare `RestProxyImpl` / `MuxwsProxyImpl` cast to the mixin interface `M`, so the object is not the consumer's own class: a custom endpoint named in `M` type-checks and is `undefined` when called. A factory-built ViewSet is the consumer's class, so its own methods exist.
