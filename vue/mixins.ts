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
 * and a subclass may then not override an action with a method (TS2425). Overriding one to add
 * caching or reshape parameters works on a hand-written proxy subclass today, and must keep
 * working here.
 *
 * A composite mixin restates nothing: its interface extends the leaves its `actions` spread names,
 * so each action's signature exists in exactly one place.
 *
 * The methods have no implementation anywhere in this file. The implementation is one HTTP call in
 * ViewSetProxyBase, and a mixin carrying its own would be a second copy of it.
 */

/*
 * The two rules below object to precisely what this file is for.
 *
 * no-unsafe-declaration-merging guards against a class and an interface merging by accident, where
 * the interface promises members the constructor never assigns. Here it is the design: the members
 * are implemented by ViewSetProxyBase, and a mixin that assigned them would be a second copy of the
 * implementation.
 *
 * no-unused-vars fires on the class half's type parameters, which only the interface half uses. They
 * cannot be dropped: declaration merging requires both halves to declare identical type parameters.
 */
/* eslint-disable @typescript-eslint/no-unsafe-declaration-merging, @typescript-eslint/no-unused-vars */

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
  create(data: Omit<T, PK>): Promise<T>;
}
export class CreateMixin<T, PK extends keyof T> {
  static readonly actions: readonly string[] = ['create'];
}

export interface BulkOnlyCreateMixin<T, PK extends keyof T> {
  bulkCreate(data: Omit<T, PK>[]): Promise<T[]>;
}
export class BulkOnlyCreateMixin<T, PK extends keyof T> {
  static readonly actions: readonly string[] = ['bulkCreate'];
}

export interface BulkCreateMixin<T, PK extends keyof T> extends CreateMixin<T, PK>, BulkOnlyCreateMixin<T, PK> {}
export class BulkCreateMixin<T, PK extends keyof T> extends CreateMixin<T, PK> {
  static readonly actions: readonly string[] = [...CreateMixin.actions, ...BulkOnlyCreateMixin.actions];
}

export interface ListMixin<T> {
  list(params?: ListParams): Promise<T[]>;
}
export class ListMixin<T> {
  static readonly actions: readonly string[] = ['list'];
}

/** Query parameters a list call accepts. `sort` is 'column:asc,other:desc'; the rest are filters. */
export interface ListParams {
  sort?: string;
  [key: string]: string | number | boolean | null | undefined | Array<string | number>;
}

/**
 * One page, mirroring the BE PaginatedList.
 *
 * `count` is null when the backend could not know it without draining a lazy source. `hasMore` and
 * `hasPrevious` are stated rather than inferred — a client that guesses from a null gets the guess
 * wrong exactly at the boundary where it matters.
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

/** FE counterpart of the BE CursorListMixin. See PaginatedListMixin on declaring several shapes. */
export interface CursorListMixin<T> {
  listCursor(params?: CursorParams): Promise<CursorPage<T>>;
}
export class CursorListMixin<T> {
  static readonly actions: readonly string[] = ['listCursor'];
}

/**
 * FE counterpart of the BE PaginatedListMixin. `GET {basePath}` is one endpoint answering in the
 * shape the BE viewset declared as its default; this client sends no X-List-Shape header, so a
 * ViewSet declares the mixin matching that default rather than one per shape it might want.
 */
export interface PaginatedListMixin<T> {
  listPage(params?: PageParams): Promise<PaginatedList<T>>;
}
export class PaginatedListMixin<T> {
  static readonly actions: readonly string[] = ['listPage'];
}

export interface RetrieveMixin<K extends KeyType, T> {
  retrieve(pk: K): Promise<T>;
}
export class RetrieveMixin<K extends KeyType, T> {
  static readonly actions: readonly string[] = ['retrieve'];
}

export interface UpdateMixin<K extends KeyType, T> {
  update(pk: K, data: T): Promise<T>;
  partialUpdate(pk: K, data: Partial<T>): Promise<T>;
}
export class UpdateMixin<K extends KeyType, T> {
  static readonly actions: readonly string[] = ['update', 'partialUpdate'];
}

export interface BulkOnlyUpdateMixin<K extends KeyType, T> {
  bulkUpdate(records: Record<K, T>): Promise<T[]>;
  bulkPartialUpdate(records: Record<K, Partial<T>>): Promise<T[]>;
}
export class BulkOnlyUpdateMixin<K extends KeyType, T> {
  static readonly actions: readonly string[] = ['bulkUpdate', 'bulkPartialUpdate'];
}

export interface BulkUpdateMixin<K extends KeyType, T> extends UpdateMixin<K, T>, BulkOnlyUpdateMixin<K, T> {}
export class BulkUpdateMixin<K extends KeyType, T> extends UpdateMixin<K, T> {
  static readonly actions: readonly string[] = [...UpdateMixin.actions, ...BulkOnlyUpdateMixin.actions];
}

export interface DestroyMixin<K extends KeyType> {
  destroy(pk: K): Promise<DestroyReturnData>;
}
export class DestroyMixin<K extends KeyType> {
  static readonly actions: readonly string[] = ['destroy'];
}

export interface BulkOnlyDestroyMixin<K extends KeyType> {
  bulkDestroy(pks: K[]): Promise<DestroyReturnData[]>;
}
export class BulkOnlyDestroyMixin<K extends KeyType> {
  static readonly actions: readonly string[] = ['bulkDestroy'];
}

export interface BulkDestroyMixin<K extends KeyType> extends DestroyMixin<K>, BulkOnlyDestroyMixin<K> {}
export class BulkDestroyMixin<K extends KeyType> extends DestroyMixin<K> {
  static readonly actions: readonly string[] = [...DestroyMixin.actions, ...BulkOnlyDestroyMixin.actions];
}

export interface LookupMixin {
  lookup(): Promise<LookupItem[]>;
}
export class LookupMixin {
  static readonly actions: readonly string[] = ['lookup'];
}

export interface ReadOnlyViewSetMixin<K extends KeyType, T> extends ListMixin<T>, RetrieveMixin<K, T> {}
export class ReadOnlyViewSetMixin<K extends KeyType, T> extends ListMixin<T> {
  static readonly actions: readonly string[] = [...ListMixin.actions, ...RetrieveMixin.actions];
}

export interface ViewSetMixin<K extends KeyType, T, PK extends keyof T>
  extends ReadOnlyViewSetMixin<K, T>, CreateMixin<T, PK>, UpdateMixin<K, T>, DestroyMixin<K> {}
export class ViewSetMixin<K extends KeyType, T, PK extends keyof T> extends ReadOnlyViewSetMixin<K, T> {
  static readonly actions: readonly string[] = [
    ...ReadOnlyViewSetMixin.actions,
    ...CreateMixin.actions,
    ...UpdateMixin.actions,
    ...DestroyMixin.actions,
  ];
}

export interface BulkViewSetMixin<K extends KeyType, T, PK extends keyof T>
  extends ViewSetMixin<K, T, PK>, BulkOnlyCreateMixin<T, PK>, BulkOnlyUpdateMixin<K, T>, BulkOnlyDestroyMixin<K> {}
export class BulkViewSetMixin<K extends KeyType, T, PK extends keyof T> extends ViewSetMixin<K, T, PK> {
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
 */
export type ActionSurface<K extends KeyType, T, PK extends keyof T, A> = ('create' extends A
  ? CreateMixin<T, PK>
  : unknown) &
  ('bulkCreate' extends A ? BulkOnlyCreateMixin<T, PK> : unknown) &
  ('list' extends A ? ListMixin<T> : unknown) &
  ('listPage' extends A ? PaginatedListMixin<T> : unknown) &
  ('listCursor' extends A ? CursorListMixin<T> : unknown) &
  ('retrieve' extends A ? RetrieveMixin<K, T> : unknown) &
  ('update' extends A ? UpdateMixin<K, T> : unknown) &
  ('bulkUpdate' extends A ? BulkOnlyUpdateMixin<K, T> : unknown) &
  ('destroy' extends A ? DestroyMixin<K> : unknown) &
  ('bulkDestroy' extends A ? BulkOnlyDestroyMixin<K> : unknown) &
  ('lookup' extends A ? LookupMixin : unknown);

/** Every action name there is: `A = string` selects all of them. */
export type ActionName = keyof ActionSurface<KeyType, unknown, never, string>;
