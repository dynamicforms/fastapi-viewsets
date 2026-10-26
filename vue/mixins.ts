/**
 * FE counterpart of BE mixins.py — the mixins a ViewSet declaration is composed of.
 *
 * Each mixin is an interface merged into a class of the same name. The interface names the actions
 * and their signatures; the class carries the `actions` list that the schema check reads at
 * runtime. Both halves are needed because neither alone can do the job: a TypeScript `implements`
 * clause is erased before anything runs, and a runtime list of strings says nothing about types.
 *
 *   class ItemViewSet extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin, LookupMixin]) {}
 *
 * The list is written once, as values. `restViewSet` reads the action names off the mixins'
 * instance types to build the ViewSet's public surface, and hands the same list to the proxy so
 * that the schema check can compare it against the BE.
 *
 * The members are methods rather than properties, and are reached through an intersection rather
 * than a `Pick<>` of a lookup table: a mapped type re-emits a method as a function-valued property,
 * and a subclass may then not override an action with a method (TS2425). A subclass overrides an
 * action to add caching or reshape parameters, on a hand-written proxy and on a factory-built one.
 *
 * A composite mixin restates nothing: its interface extends the leaves its `actions` spread names,
 * so each action's signature exists in exactly one place.
 *
 * The methods have no implementation anywhere in this file. The implementation is one HTTP call in
 * ViewSetProxyBase, and a mixin carrying its own would be a second copy of it.
 */

/*
 * no-unsafe-declaration-merging guards against a class and an interface merging by accident, where
 * the interface promises members the constructor never assigns. Here it is the design: the members
 * are implemented by ViewSetProxyBase, and a mixin that assigned them would be a second copy of the
 * implementation.
 *
 * no-unused-vars fires on the class half's type parameters, which only the interface half uses. They
 * cannot be dropped: declaration merging requires both halves to declare identical type parameters.
 */
/* eslint-disable @typescript-eslint/no-unsafe-declaration-merging, @typescript-eslint/no-unused-vars */

import type { ViewSetActionCall } from './call';
import type { SortState } from './sort';

export interface LookupItem {
  group: unknown;
  pk: unknown;
  title: string;
  icon: string | null;
}

export type KeyType = string | number;
export type DestroyReturnData = Record<KeyType, any>;
// ---------------------------------------------------------------------------
// Individual operation mixins
// ---------------------------------------------------------------------------

export interface CreateMixin<T, PK extends keyof T> {
  create(data: Omit<T, PK>): ViewSetActionCall<T>;
}
export class CreateMixin<T, PK extends keyof T> {
  static readonly actions: readonly string[] = ['create'];
}

export interface BulkOnlyCreateMixin<T, PK extends keyof T> {
  bulkCreate(data: Omit<T, PK>[]): ViewSetActionCall<T[]>;
}
export class BulkOnlyCreateMixin<T, PK extends keyof T> {
  static readonly actions: readonly string[] = ['bulkCreate'];
}

export interface BulkCreateMixin<T, PK extends keyof T> extends CreateMixin<T, PK>, BulkOnlyCreateMixin<T, PK> {}
export class BulkCreateMixin<T, PK extends keyof T> extends CreateMixin<T, PK> {
  static readonly actions: readonly string[] = [...CreateMixin.actions, ...BulkOnlyCreateMixin.actions];
}

/**
 * The row types a BE viewset's list adds through `MultiTypeListMixin`, as `{ wireName: RowType }`;
 * `{}` when it adds none.
 */
export type ListTypes = object;

/** The wire names of the list types `V`, the values `ListOptions.listType` accepts. */
export type ListTypeName<V extends ListTypes> = keyof V & string;

/**
 * What a list call that sends `listType` `N` answers with: `V[N]`. `T` when `V` names no type, so
 * that a subclass of a ViewSet without list types overrides `list()` with a method that is not
 * generic.
 */
export type ListRow<T, V extends ListTypes, N extends ListTypeName<V>> = [keyof V] extends [never] ? T : V[N];

/**
 * What a list call whose `listType` may be absent answers with: `T` or any of `V`'s row types. The
 * type of a call whose options are only known as `ListOptions`.
 */
export type AnyListRow<T, V extends ListTypes> = T | V[ListTypeName<V>];

/** The options of `list()`, `listPage()` and `listCursor()`. */
export interface ListOptions<N extends string = string> {
  /**
   * The row type the BE answers with, by its wire name, sent as `X-List-Type`. Without it the
   * header is not sent and the BE answers with its default row type.
   */
  listType?: N;
}

/** The options of a list call that selects the row type `N`: `listType` is required. */
export interface ListTypeOptions<N extends string> extends ListOptions<N> {
  listType: N;
}

/** The key under which a list mixin carries its row types. Type-level only: no value has it. */
export declare const LIST_ROWS: unique symbol;

/**
 * The row types of a list mixin: `T` for a call without `listType`, `V` for the named ones. A
 * phantom member, never set; `ViewSetCalls` and `watch()` read the types off it, because a generic
 * method's type parameters are lost when its type is inferred.
 */
export interface ListRows<T, V extends ListTypes> {
  readonly [LIST_ROWS]?: { readonly row: T; readonly types: V };
}

/** The `ListRows` member of `M`, `{ row, types }`; `never` when `M` has none. */
export type ListRowsOf<M> = typeof LIST_ROWS extends keyof M ? NonNullable<M[typeof LIST_ROWS]> : never;

/**
 * FE counterpart of the BE ListMixin. `T` is the row type a call without `listType` answers with;
 * `V` maps the wire name of every row type the BE viewset adds with `MultiTypeListMixin` to that
 * type, and `options.listType` selects one of them:
 *
 * - no `listType`: `T`;
 * - a `listType` literal of `V`: that type, `V[N]`;
 * - options typed `ListOptions`, whose `listType` may be absent: `AnyListRow<T, V>`.
 *
 * A `listType` that `V` does not name does not compile.
 */
export interface ListMixin<T, V extends ListTypes = {}> extends ListRows<T, V> {
  list(params?: ListParams, options?: ListOptions<never>): ViewSetActionCall<T[]>;
  list<N extends ListTypeName<V>>(
    params: ListParams | undefined,
    options: ListTypeOptions<N>,
  ): ViewSetActionCall<ListRow<T, V, N>[]>;
  list(params?: ListParams, options?: ListOptions<ListTypeName<V>>): ViewSetActionCall<AnyListRow<T, V>[]>;
}
export class ListMixin<T, V extends ListTypes = {}> {
  static readonly actions: readonly string[] = ['list'];
}

/**
 * Query parameters a list call accepts. `sort` is 'column:asc,other:desc' (`formatSort` writes it
 * from a `SortState`); the rest are filters. Without `sort` the BE applies its `default_sort`; `''`
 * leaves the list unordered.
 */
export interface ListParams {
  sort?: string;
  [key: string]: string | number | boolean | null | undefined | Array<string | number>;
}

/**
 * What a ViewSet declaring `list`, `listPage` or `listCursor` has besides those actions. Not an
 * action: it reads the BE viewset's schema rather than calling an endpoint of its own.
 */
export interface ListDefaultSort {
  /**
   * The BE viewset's `default_sort`: the ordering its list endpoint applies when a call sends no
   * `sort`, e.g. a grid's initial `sortState`. `[]` when it declares none. Read from the `sort`
   * parameter's default in the viewset's `/schema`, which the ViewSet fetches once and shares with
   * the startup schema check. When the shared request fails, it issues one of its own, repeated as
   * any other call is; when that fails too, it rejects, and the next call fetches again.
   */
  defaultSort(): Promise<SortState>;
}

/** The actions that read the list endpoint. */
export type ListAction = 'list' | 'listPage' | 'listCursor';

/** `ListDefaultSort` when the member names `A` include a list action, `unknown` otherwise. */
export type DefaultSortOf<A> = [Extract<A, ListAction>] extends [never] ? unknown : ListDefaultSort;

/**
 * One page, mirroring the BE PaginatedList.
 *
 * `count` is null when the backend could not know it without draining a lazy source. `hasMore` and
 * `hasPrevious` are sent by the backend; a null `count` does not determine them.
 */
export interface PaginatedList<T> {
  results: T[];
  offset: number;
  limit: number | null;
  count: number | null;
  hasMore: boolean;
  hasPrevious: boolean;
}

export interface PageParams extends ListParams {
  offset?: number;
  limit?: number;
}

/**
 * One cursor page.
 *
 * `next`/`previous` are exclusive, so following them never repeats a row. `first`/`last` are the
 * same two edges read inclusively: they return their own row again — one duplicate to drop — and
 * in exchange they survive rows being inserted at that edge, which is what polling a live list
 * needs. They are present whenever the page is non-empty, even when `next` is null.
 *
 * There is no total count: producing one costs a second full pass per request and is stale by the
 * time it is read.
 */
export interface CursorPage<T> {
  results: T[];
  limit: number;
  hasMore: boolean;
  hasPrevious: boolean;
  next: string | null;
  previous: string | null;
  first: string | null;
  last: string | null;
}

export interface CursorParams extends ListParams {
  cursor?: string;
  limit?: number;
}

/**
 * FE counterpart of the BE CursorListMixin. See PaginatedListMixin on declaring several shapes and
 * ListMixin on `T` and `V`.
 */
export interface CursorListMixin<T, V extends ListTypes = {}> extends ListRows<T, V> {
  listCursor(params?: CursorParams, options?: ListOptions<never>): ViewSetActionCall<CursorPage<T>>;
  listCursor<N extends ListTypeName<V>>(
    params: CursorParams | undefined,
    options: ListTypeOptions<N>,
  ): ViewSetActionCall<CursorPage<ListRow<T, V, N>>>;
  listCursor(
    params?: CursorParams,
    options?: ListOptions<ListTypeName<V>>,
  ): ViewSetActionCall<CursorPage<AnyListRow<T, V>>>;
}
export class CursorListMixin<T, V extends ListTypes = {}> {
  static readonly actions: readonly string[] = ['listCursor'];
}

/**
 * FE counterpart of the BE PaginatedListMixin. `list()`, `listPage()` and `listCursor()` read the same
 * `GET {basePath}` and each requests its shape with `X-List-Shape`, so a ViewSet declares one mixin
 * per shape the BE viewset offers through `list_shape` and `list_shapes`. The row types (`T`, `V`,
 * see ListMixin) are independent of the shape.
 */
export interface PaginatedListMixin<T, V extends ListTypes = {}> extends ListRows<T, V> {
  listPage(params?: PageParams, options?: ListOptions<never>): ViewSetActionCall<PaginatedList<T>>;
  listPage<N extends ListTypeName<V>>(
    params: PageParams | undefined,
    options: ListTypeOptions<N>,
  ): ViewSetActionCall<PaginatedList<ListRow<T, V, N>>>;
  listPage(
    params?: PageParams,
    options?: ListOptions<ListTypeName<V>>,
  ): ViewSetActionCall<PaginatedList<AnyListRow<T, V>>>;
}
export class PaginatedListMixin<T, V extends ListTypes = {}> {
  static readonly actions: readonly string[] = ['listPage'];
}

export interface RetrieveMixin<K extends KeyType, T> {
  retrieve(pk: K): ViewSetActionCall<T>;
}
export class RetrieveMixin<K extends KeyType, T> {
  static readonly actions: readonly string[] = ['retrieve'];
}

export interface UpdateMixin<K extends KeyType, T> {
  update(pk: K, data: T): ViewSetActionCall<T>;
  partialUpdate(pk: K, data: Partial<T>): ViewSetActionCall<T>;
}
export class UpdateMixin<K extends KeyType, T> {
  static readonly actions: readonly string[] = ['update', 'partialUpdate'];
}

export interface BulkOnlyUpdateMixin<K extends KeyType, T> {
  bulkUpdate(records: Record<K, T>): ViewSetActionCall<T[]>;
  bulkPartialUpdate(records: Record<K, Partial<T>>): ViewSetActionCall<T[]>;
}
export class BulkOnlyUpdateMixin<K extends KeyType, T> {
  static readonly actions: readonly string[] = ['bulkUpdate', 'bulkPartialUpdate'];
}

export interface BulkUpdateMixin<K extends KeyType, T> extends UpdateMixin<K, T>, BulkOnlyUpdateMixin<K, T> {}
export class BulkUpdateMixin<K extends KeyType, T> extends UpdateMixin<K, T> {
  static readonly actions: readonly string[] = [...UpdateMixin.actions, ...BulkOnlyUpdateMixin.actions];
}

export interface DestroyMixin<K extends KeyType> {
  destroy(pk: K): ViewSetActionCall<DestroyReturnData>;
}
export class DestroyMixin<K extends KeyType> {
  static readonly actions: readonly string[] = ['destroy'];
}

export interface BulkOnlyDestroyMixin<K extends KeyType> {
  bulkDestroy(pks: K[]): ViewSetActionCall<DestroyReturnData[]>;
}
export class BulkOnlyDestroyMixin<K extends KeyType> {
  static readonly actions: readonly string[] = ['bulkDestroy'];
}

export interface BulkDestroyMixin<K extends KeyType> extends DestroyMixin<K>, BulkOnlyDestroyMixin<K> {}
export class BulkDestroyMixin<K extends KeyType> extends DestroyMixin<K> {
  static readonly actions: readonly string[] = [...DestroyMixin.actions, ...BulkOnlyDestroyMixin.actions];
}

/**
 * FE counterpart of the BE LookupMixin. `L` is the BE viewset's lookup item type; `params` are the
 * fields of its lookup filter model, sent as query parameters.
 */
export interface LookupMixin<L = LookupItem> {
  lookup(params?: LookupParams): ViewSetActionCall<L[]>;
}
export class LookupMixin<L = LookupItem> {
  static readonly actions: readonly string[] = ['lookup'];
}

/** Query parameters a lookup call accepts: `q` and whatever else the BE lookup filter model declares. */
export interface LookupParams {
  q?: string;
  [key: string]: string | number | boolean | null | undefined | Array<string | number>;
}

/**
 * `V` and `R` are the list's row types (ListMixin's `V` and `T`): `R` is what a call without
 * `listType` answers with, default `T`.
 */
export interface ReadOnlyViewSetMixin<K extends KeyType, T, V extends ListTypes = {}, R = T>
  extends ListMixin<R, V>, RetrieveMixin<K, T> {}
export class ReadOnlyViewSetMixin<K extends KeyType, T, V extends ListTypes = {}, R = T> extends ListMixin<R, V> {
  static readonly actions: readonly string[] = [...ListMixin.actions, ...RetrieveMixin.actions];
}

/** See ReadOnlyViewSetMixin on `V` and `R`. */
export interface ViewSetMixin<K extends KeyType, T, PK extends keyof T, V extends ListTypes = {}, R = T>
  extends ReadOnlyViewSetMixin<K, T, V, R>, CreateMixin<T, PK>, UpdateMixin<K, T>, DestroyMixin<K> {}
export class ViewSetMixin<
  K extends KeyType,
  T,
  PK extends keyof T,
  V extends ListTypes = {},
  R = T,
> extends ReadOnlyViewSetMixin<K, T, V, R> {
  static readonly actions: readonly string[] = [
    ...ReadOnlyViewSetMixin.actions,
    ...CreateMixin.actions,
    ...UpdateMixin.actions,
    ...DestroyMixin.actions,
  ];
}

/** See ReadOnlyViewSetMixin on `V` and `R`. */
export interface BulkViewSetMixin<K extends KeyType, T, PK extends keyof T, V extends ListTypes = {}, R = T>
  extends
    ViewSetMixin<K, T, PK, V, R>,
    BulkOnlyCreateMixin<T, PK>,
    BulkOnlyUpdateMixin<K, T>,
    BulkOnlyDestroyMixin<K> {}
export class BulkViewSetMixin<
  K extends KeyType,
  T,
  PK extends keyof T,
  V extends ListTypes = {},
  R = T,
> extends ViewSetMixin<K, T, PK, V, R> {
  static readonly actions: readonly string[] = [
    ...ViewSetMixin.actions,
    ...BulkOnlyCreateMixin.actions,
    ...BulkOnlyUpdateMixin.actions,
    ...BulkOnlyDestroyMixin.actions,
  ];
}

// ---------------------------------------------------------------------------
// The action vocabulary
// ---------------------------------------------------------------------------

/**
 * The actions named by `A`, each with the signature the mixin that contributes it declares. One
 * conditional per single-action mixin; the composites are absent on purpose, so that any action's
 * signature is written in exactly one place.
 *
 * An intersection rather than `Pick<>` of a table: a mapped type turns a method into a property,
 * and a subclass cannot then override an action with a method (TS2425).
 *
 * `L` is the lookup item type; `V` and `R` are the row types of `list`, `listPage` and `listCursor`
 * (see ReadOnlyViewSetMixin).
 */
export type ActionSurface<
  K extends KeyType,
  T,
  PK extends keyof T,
  A,
  L = LookupItem,
  V extends ListTypes = {},
  R = T,
> = ('create' extends A ? CreateMixin<T, PK> : unknown) &
  ('bulkCreate' extends A ? BulkOnlyCreateMixin<T, PK> : unknown) &
  ('list' extends A ? ListMixin<R, V> : unknown) &
  ('listPage' extends A ? PaginatedListMixin<R, V> : unknown) &
  ('listCursor' extends A ? CursorListMixin<R, V> : unknown) &
  ('retrieve' extends A ? RetrieveMixin<K, T> : unknown) &
  ('update' extends A ? UpdateMixin<K, T> : unknown) &
  ('bulkUpdate' extends A ? BulkOnlyUpdateMixin<K, T> : unknown) &
  ('destroy' extends A ? DestroyMixin<K> : unknown) &
  ('bulkDestroy' extends A ? BulkOnlyDestroyMixin<K> : unknown) &
  ('lookup' extends A ? LookupMixin<L> : unknown);

/** Every action name there is: `A = string` selects all of them. */
export type ActionName = Exclude<keyof ActionSurface<KeyType, unknown, never, string>, typeof LIST_ROWS>;
