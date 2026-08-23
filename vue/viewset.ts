/**
 * The ViewSet class factory — FE counterpart of listing mixins in a BE viewset's bases.
 *
 *   class ItemApi extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin, LookupMixin]) {
 *     async count(): Promise<number> {
 *       return this.request<number>('GET', '/count');
 *     }
 *   }
 *
 * The mixin list is written once, as values, and does three jobs: it decides which actions the
 * class exposes to callers, it types them, and it is handed to the proxy so the startup schema
 * check can compare it against the BE. Calling an action the ViewSet did not declare is a compile
 * error rather than a 404 at runtime.
 *
 * This is what `route_rest` cannot do. That returns an instance cast to a mixin interface, so the
 * type narrows but the object is a bare proxy: a custom endpoint declared in the interface type-
 * checks and is undefined when called. Here the object is the consumer's own class, so its own
 * methods exist.
 */

import type { ActionName, ActionSurface, KeyType } from './mixins';
import { MuxwsProxyImpl, type MuxwsProxyOptions } from './muxws-proxy';
import { FACTORY_BUILT, type ProxyBaseOptions, ViewSetInternals, type ViewSetMixinDeclaration } from './proxy-base';
import { RestProxyImpl, type RestProxyOptions } from './rest-proxy';

/** A mixin class: the runtime `actions` the schema check reads, and the type naming those actions. */
export type ViewSetMixinClass = ViewSetMixinDeclaration & (abstract new (...args: any[]) => object);

/**
 * The actions a mixin class names, read off its instance type.
 *
 * The instance type rather than the `actions` tuple, because a tuple would have to be `as const` on
 * every mixin, and a `readonly ['create']` static cannot then be inherited by a composite whose own
 * static is a different tuple (TS2417). `D` is naked so that a union of mixins distributes.
 */
type ActionsOf<D> = D extends abstract new (...args: any[]) => infer I ? Extract<keyof I, ActionName> : never;

/** The fields of `T` that could be a primary key. */
export type PkFieldName<T> = Extract<
  { [F in keyof T]-?: NonNullable<T[F]> extends KeyType ? F : never }[keyof T],
  string
>;

type PkType<T, PK extends keyof T> = NonNullable<T[PK]> & KeyType;

/**
 * Brands a factory-built class's `declares` so a subclass restating it fails on one flat "missing
 * property" line naming `declares` itself, rather than TypeScript recursing into which method each
 * mixin in the plain, unbranded replacement array is missing relative to the original. A record
 * type wrapping `D` behind the phantom key, not `D & {brand}`: an intersection would still expose
 * `D`'s own array shape to the comparison and recurse into it exactly as before; a plain array
 * literal has no property at all under this key, so the mismatch stops at the top. Erased at
 * runtime - `bindViewSet` casts through `unknown`, so the actual `static declares` stays a plain
 * array, and every internal reader (rest-proxy.ts, muxws-proxy.ts, proxy-base.ts's
 * `declaredActions`) already reads it through its own cast rather than this type.
 *
 * `FACTORY_BUILT` lives in proxy-base.ts, not here, so `route_rest`/`route_muxws` can check for the
 * same brand without importing from this module - see proxy-base.ts's doc comment on the symbol.
 */
type FactoryDeclares<D extends readonly ViewSetMixinClass[]> = { readonly [FACTORY_BUILT]: D };

/**
 * What the factory hands back: a class to extend.
 *
 * A `declares` list naming no action — `[]`, or one annotated `ViewSetMixinClass[]`, which erases
 * which mixins are in it — would otherwise produce a ViewSet with no actions and no complaint.
 * TS2507 prints the type it was given, so the type is the sentence.
 */
export type ViewSetClass<T, PK extends keyof T, D extends readonly ViewSetMixinClass[], O extends ProxyBaseOptions> = [
  ActionsOf<D[number]>,
] extends [never]
  ? 'declares must name at least one action: pass the mixin classes themselves, unannotated'
  : {
      new (
        options: Omit<O, 'pkFieldName' | 'declares'>,
      ): ViewSetInternals & ActionSurface<PkType<T, PK>, T, PK, ActionsOf<D[number]>>;
      readonly declares: FactoryDeclares<D>;
    };

/**
 * `any` on the constructor because a class expression may only extend a constructor whose members
 * are statically known, and the two transports' options differ. The real type is put back on by the
 * return type of whichever factory calls this.
 */
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
      // Injected into the argument expression rather than assigned afterwards: under
      // useDefineForClassFields an assignment would emit a [[Define]] that runs after super() and
      // would clobber what the base constructor had just set.
      //
      // `declares: undefined` so that the static above is what the check reads. A subclass stating
      // its own `static declares` then shadows it by ordinary static lookup, as it always has.
      super({ ...options, pkFieldName, declares: undefined });
    }
  };
}

/**
 * A REST ViewSet base class.
 *
 * The empty `()` is not decoration: TypeScript has no partial type-argument inference, so the model
 * cannot be given explicitly while the pk field and the mixin list are inferred from arguments in
 * the same call (TS2558). Currying is the only way to have both.
 */
export function restViewSet<T>() {
  return <PK extends PkFieldName<T> & keyof T, D extends readonly ViewSetMixinClass[]>(
    pkFieldName: PK,
    declares: D,
  ): ViewSetClass<T, PK, D, RestProxyOptions> =>
    bindViewSet(RestProxyImpl, pkFieldName, declares) as unknown as ViewSetClass<T, PK, D, RestProxyOptions>;
}

/** A muxws ViewSet base class. See `restViewSet` for why the call is curried. */
export function muxwsViewSet<T>() {
  return <PK extends PkFieldName<T> & keyof T, D extends readonly ViewSetMixinClass[]>(
    pkFieldName: PK,
    declares: D,
  ): ViewSetClass<T, PK, D, MuxwsProxyOptions> =>
    bindViewSet(MuxwsProxyImpl, pkFieldName, declares) as unknown as ViewSetClass<T, PK, D, MuxwsProxyOptions>;
}
