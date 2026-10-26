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

The mixin list is written once, as values, and does three jobs: it decides which actions the class exposes to callers,
it types them, and it is handed to the proxy so the startup schema check can compare it against the BE.

## Signatures

```ts
function restViewSet<T, L = LookupItem, V extends ListTypes = {}, R = T>(): <
  PK extends PkFieldName<T> & keyof T,
  D extends readonly ViewSetMixinClass[],
>(pkFieldName: PK, declares: D) => ViewSetClass<T, PK, D, RestProxyOptions, L, V, R>

function muxwsViewSet<T, L = LookupItem, V extends ListTypes = {}, R = T>(): <
  PK extends PkFieldName<T> & keyof T,
  D extends readonly ViewSetMixinClass[],
>(pkFieldName: PK, declares: D) => ViewSetClass<T, PK, D, MuxwsProxyOptions, L, V, R>
```

`L` is the item type `lookup()` answers with, the BE viewset's `TLookupItem`. `V` and `R` are the
row types of `list()`, `listPage()` and `listCursor()` ([list types](./vue-mixins#list-types)): `V`
maps the wire name of every row type the BE viewset adds with
[`MultiTypeListMixin`](./python-mixins#multitypelistmixin-ts) to that type, and a call selects one with
`{ listType }`; `R` is the row type of a call without `listType` — `T` when the BE viewset composes a
`ListMixin`, the first added type when it does not.

```ts
interface Member { id: number; name: string; email: string; address: string }
interface MemberRow { id: number; name: string }

class MemberApi extends restViewSet<Member, LookupItem, { compact: MemberRow }>()('id', [ViewSetMixin]) {}

const members = new MemberApi({ basePath: '/members' });
await members.list(); // Member[]
await members.list({ sort: 'name:asc' }, { listType: 'compact' }); // MemberRow[]
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

/** The pk type of the model's pk field. */
type PkType<T, PK extends keyof T> = NonNullable<T[PK]> & KeyType;

type ViewSetClass<
  T,
  PK extends keyof T,
  D extends readonly ViewSetMixinClass[],
  O extends ProxyBaseOptions,
  L = LookupItem,
  V extends ListTypes = {},
  R = T,
> =
  // `[]` builds a ViewSet with no standard action; a list whose mixins name no action resolves to
  // its own error message, see TS2507 below.
  [D[number]] extends [never]
    ? ViewSetClassOf<T, PK, D, O, L, V, R>
    : [ActionsOf<D[number]>] extends [never]
      ? 'declares must name at least one action: pass the mixin classes themselves, unannotated'
      : ViewSetClassOf<T, PK, D, O, L, V, R>;

type ViewSetClassOf<
  T,
  PK extends keyof T,
  D extends readonly ViewSetMixinClass[],
  O extends ProxyBaseOptions,
  L,
  V extends ListTypes,
  R,
> = {
  new (options: Omit<O, 'pkFieldName' | 'declares'>): ViewSetInternals &
    ActionSurface<PkType<T, PK>, T, PK, ActionsOf<D[number]>, L, V, R> &
    DefaultSortOf<ActionsOf<D[number]>> &
    ViewSetWatch<R, PkType<T, PK>, FetchOption<R, T, PkType<T, PK>>> & { readonly pkFieldName: PK };
  readonly declares: FactoryDeclares<D>;
};

/** The factory's `declares`, branded so a subclass restating it fails on one line (TS2417). */
type FactoryDeclares<D extends readonly ViewSetMixinClass[]> = { readonly [FACTORY_BUILT]: D };
```

## Why the call is curried

The empty `()` in `restViewSet<Item>()('id', [...])` is required. TypeScript has no partial type-argument inference: in
a single call, the model cannot be given explicitly while the pk field and the mixin list are inferred from arguments.
Naming only `T` on a three-parameter signature is `TS2558: Expected 3 type arguments, but got 1`. Two calls is the only
way to have both.

## Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `T` (type argument) | — | The record model, e.g. `Item`. Explicit, on the first call. |
| `L` (type argument) | — | The lookup item type. Default `LookupItem`. |
| `V` (type argument) | — | The list types added by the BE viewset's `MultiTypeListMixin`, `{ wireName: RowType }`. Default `{}`. |
| `R` (type argument) | — | The row type of a list call without `listType`. Default `T`. |
| `pkFieldName` | `PkFieldName<T> & keyof T` | Name of the PK field on the model, e.g. `'id'`. An argument, not a type argument: it is checked against the model's fields, and the pk *type* is read off `T[PK]`. |
| `declares` | `readonly ViewSetMixinClass[]` | The mixin classes themselves, as values. |

`retrieve`, `update`, `destroy` and the bulk actions take a pk of the type the model gives it — with
`Item['id']: number`, `api.retrieve('1')` is
`TS2345: Argument of type 'string' is not assignable to parameter of type 'number'`.

## Constructor options

The returned class binds `pkFieldName` and `declares` itself, so its constructor takes what is left:

```ts
// restViewSet
interface Options {
  basePath: string;
  validateSchema?: boolean;
  repeat?: RepeatOptions;
  events?: EventClient;
  topic?: string;
  axiosInstance?: AxiosInstance;
  attemptTimeoutMs?: number;
}

// muxwsViewSet
interface Options {
  basePath: string;
  validateSchema?: boolean;
  repeat?: RepeatOptions;
  events?: EventClient;
  topic?: string;
  peer: MuxwsPeerSource;
  timeoutMs?: number;
  headers?: Record<string, string>;
}
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `basePath` | `string` | — | Base path to the resource, e.g. `'/items'`. A trailing slash is stripped. |
| `validateSchema` | `boolean` | `true` | Set `false` to skip the startup schema check (it is advisory and costs one request). |
| `repeat` | [`RepeatOptions`](./idempotency#repeatoptions) | `{ withinMs: 600000, delayMs: 1000 }` | When a call whose attempt ended without an answer is repeated; see [Idempotent Calls](../guide/idempotency#repeats). |
| `events` | `EventClient` | — | The event client [`watch()`](./vue-mixins#watch) subscribes on. Without it, a muxws ViewSet uses the client registered for its peer and a REST ViewSet throws from `watch()`. |
| `topic` | `string` | `basePath` | The client topic `watch()` subscribes: the BE path relative to the application's `root_path`, without a Starlette `Mount` prefix. |
| `axiosInstance` | `AxiosInstance` | global `axios` | REST only. |
| `attemptTimeoutMs` | `number` | `30000` | REST only. Timeout of one attempt, passed to axios as `timeout`; `0` for none. |
| `peer` | `MuxwsPeerSource` | — | muxws only. A `Peer`, or a function returning one or a promise of one; the function is called again after its peer closed for good, and after it threw or rejected (see [route_muxws](./route-muxws#muxwsproxyoptions)). |
| `timeoutMs` | `number` | — | muxws only. Per-attempt timeout, covering the wait for the reply headers and for the body; muxws resets the stream with `TIMEOUT` when it expires and the call is repeated, or rejects with its `StreamTimeout` when it is not. |
| `headers` | `Record<string, string>` | `{}` | muxws only. Headers added to every request; a call's `RequestOptions.headers` override them. |

## watch()

Every class the factory builds has `watch(options)`, whatever `declares` holds, typed as
`ViewSetWatch<R, PkType<T, PK>, FetchOption<R, T, PkType<T, PK>>>`: `onChange` receives
`RecordsChanged<R, PkType<T, PK>>`, the rows of a list call without `listType`. It subscribes
`basePath` to `records_changed` on the `events` client of the options; without one, a
`muxwsViewSet` class uses the client registered for its peer, and a `restViewSet` class throws.
With `fetch: true` it calls `retrieve(pk)` of the proxy underneath, whether or not `declares` names
`RetrieveMixin`; that form compiles only when `R` is `T`.
With `fetch` as a function `(pks) => Promise<readonly Row[]>`, `onChange` receives the `Row`s that
function returns, whatever `R` is: a list type is watched by fetching with its `listType`. See
[`watch()`](./vue-mixins#watch).

## Inside the class body

A ViewSet's own methods reach `this.request()` and `this.basePath`, inherited from `ViewSetInternals`:

```ts
protected readonly basePath: string;
protected request<R>(method: HttpMethod, path: string, options?: RequestOptions): ViewSetCall<R>;
```

`path` is relative to `basePath` — `''` for the collection, `/1` for a record. Writing a custom endpoint against
`request()` rather than against a transport is what lets the same method body serve a REST and a muxws ViewSet.

Both members are `protected`, so a caller cannot touch them: `api.basePath` outside the class is
`TS2445: Property 'basePath' is protected and only accessible within class 'ViewSetInternals' and its subclasses`.

`this.http` is **not** available on a factory-built class — the factory types its result as `ViewSetInternals` plus the
declared actions, and the axios instance is not part of that. A method that needs axios directly extends `RestProxyImpl`
instead:

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
| `TS2507` | `declares` is an array annotated `ViewSetMixinClass[]`, which erases which mixins are in it | *Type `'declares must name at least one action: pass the mixin classes themselves, unannotated'` is not a constructor function type.* The message is the return type: a ViewSet with no actions would otherwise be built without complaint. An empty literal, `[]`, is accepted: it builds a ViewSet with custom endpoints only. |
| `TS2558` | `restViewSet<Item, 'id', [...]>(...)` | *Expected 1 type arguments, but got 3.* The call is curried; see [above](#why-the-call-is-curried). |

## Schema check

On construction, unless `validateSchema: false`, the ViewSet fetches `GET {basePath}/schema` over its own transport and
compares the declaration against it, `console.warn`-ing any disagreement: an action declared that the BE does not serve,
an endpoint served that the ViewSet declares no action for, and a custom BE endpoint the ViewSet has no method for — the
BE method's name (`x-viewset-action`) in camelCase, or the name itself. It is advisory — failures to fetch or parse are
ignored.

## Relation to route_rest / route_muxws

[`route_rest`](/api/route-rest) and `route_muxws` are the other form. They return a bare `RestProxyImpl` /
`MuxwsProxyImpl` cast to the mixin interface `M`, so the object is not the consumer's own class: a custom endpoint named
in `M` type-checks and is `undefined` when called. A factory-built ViewSet is the consumer's class, so its own methods
exist.

Passing a factory-built class to either fails to compile rather than silently building an object missing that class's
own methods: the overload that matches a factory-built class returns a self-describing string type instead of a usable
proxy, so the error surfaces as `TS2339` on the first property or method accessed on the result
(`Property 'list' does not exist on type '"route_rest cannot take a factory-built class - extend it directly instead"'`),
not on the `route_rest`/`route_muxws` call itself.
