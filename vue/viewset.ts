/**
 * The ViewSet class factory — FE counterpart of listing mixins in a BE viewset's bases:
 *
 *   class ItemApi extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin, LookupMixin]) { ... }
 *
 * The mixin list selects and types the exposed actions and feeds the startup schema check; calling
 * an undeclared action is a compile error. Unlike `route_rest`, the object is the consumer's own
 * class, so its custom methods exist.
 */

import type { ViewSetWatch } from './events/changes';
import type { ActionName, ActionSurface, DefaultSortOf, KeyType, LookupItem } from './mixins';
import { MuxwsProxyImpl, type MuxwsProxyOptions } from './muxws-proxy';
import { FACTORY_BUILT, type ProxyBaseOptions, ViewSetInternals, type ViewSetMixinDeclaration } from './proxy-base';
import { RestProxyImpl, type RestProxyOptions } from './rest-proxy';

/** A mixin class: the runtime `actions` the schema check reads, and the type naming those actions. */
export type ViewSetMixinClass = ViewSetMixinDeclaration & (abstract new (...args: any[]) => object);

/**
 * The actions a mixin class names, read off its instance type (an `as const` actions tuple would
 * break inheritance between mixins, TS2417). `D` is naked so that a union of mixins distributes.
 */
type ActionsOf<D> = D extends abstract new (...args: any[]) => infer I ? Extract<keyof I, ActionName> : never;

/** The fields of `T` that could be a primary key. */
export type PkFieldName<T> = Extract<
  { [F in keyof T]-?: NonNullable<T[F]> extends KeyType ? F : never }[keyof T],
  string
>;

type PkType<T, PK extends keyof T> = NonNullable<T[PK]> & KeyType;

/**
 * Brands a factory-built class's `declares`, so a subclass restating it fails with one short "missing
 * property" error. A wrapper record rather than `D & {brand}`, which would make TypeScript recurse
 * into the array. Type-only: at runtime `static declares` is a plain array.
 */
type FactoryDeclares<D extends readonly ViewSetMixinClass[]> = { readonly [FACTORY_BUILT]: D };

/**
 * What the factory hands back: a class to extend. `[]` builds a ViewSet with custom endpoints only.
 * A list annotated `ViewSetMixinClass[]` names no action; this type then resolves to a string stating
 * the fix, which TS2507 prints. `pkFieldName`, `watch()` and, with a list action, `defaultSort()` are
 * added on top of the narrow `ViewSetInternals`.
 */
export type ViewSetClass<
  T,
  PK extends keyof T,
  D extends readonly ViewSetMixinClass[],
  O extends ProxyBaseOptions,
  L = LookupItem,
> = [D[number]] extends [never]
  ? ViewSetClassOf<T, PK, D, O, L>
  : [ActionsOf<D[number]>] extends [never]
    ? 'declares must name at least one action: pass the mixin classes themselves, unannotated'
    : ViewSetClassOf<T, PK, D, O, L>;

type ViewSetClassOf<T, PK extends keyof T, D extends readonly ViewSetMixinClass[], O extends ProxyBaseOptions, L> = {
  new (
    options: Omit<O, 'pkFieldName' | 'declares'>,
  ): ViewSetInternals &
    ActionSurface<PkType<T, PK>, T, PK, ActionsOf<D[number]>, L> &
    DefaultSortOf<ActionsOf<D[number]>> &
    ViewSetWatch<T, PkType<T, PK>> & { readonly pkFieldName: PK };
  readonly declares: FactoryDeclares<D>;
};

/** `any` options, as the transports' options differ; the calling factory's return type restores the real type. */
type TransportClass = new (options: any) => any;

/** Named `ViewSet` so that a stack trace and the devtools prototype chain say something. */
function bindViewSet(
  Impl: TransportClass,
  pkFieldName: string,
  declares: readonly ViewSetMixinDeclaration[],
): TransportClass {
  return class ViewSet extends Impl {
    static declares = declares;

    constructor(options: ProxyBaseOptions) {
      // Passed in the options, not assigned after super(): useDefineForClassFields would clobber the
      // base constructor's value. `declares: undefined` makes the check read the static above.
      super({ ...options, pkFieldName, declares: undefined });
    }
  };
}

/**
 * A REST ViewSet base class. Curried because TypeScript cannot infer the pk field and mixin list
 * while `T` is given explicitly (TS2558). `L` is the `lookup()` item type, default `LookupItem`.
 */
export function restViewSet<T, L = LookupItem>() {
  return <PK extends PkFieldName<T> & keyof T, D extends readonly ViewSetMixinClass[]>(
    pkFieldName: PK,
    declares: D,
  ): ViewSetClass<T, PK, D, RestProxyOptions, L> =>
    bindViewSet(RestProxyImpl, pkFieldName, declares) as unknown as ViewSetClass<T, PK, D, RestProxyOptions, L>;
}

/** A muxws ViewSet base class. See `restViewSet` for why the call is curried and what `L` is. */
export function muxwsViewSet<T, L = LookupItem>() {
  return <PK extends PkFieldName<T> & keyof T, D extends readonly ViewSetMixinClass[]>(
    pkFieldName: PK,
    declares: D,
  ): ViewSetClass<T, PK, D, MuxwsProxyOptions, L> =>
    bindViewSet(MuxwsProxyImpl, pkFieldName, declares) as unknown as ViewSetClass<T, PK, D, MuxwsProxyOptions, L>;
}
