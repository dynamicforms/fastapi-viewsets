/**
 * FE counterpart of BE mixins.py — the mixins a ViewSet declaration is composed of.
 *
 * Each mixin is an interface (action signatures) merged into a class (the runtime `actions` list the schema
 * check reads). Members are methods, not mapped-type properties, so a subclass can override an action with a
 * method (TS2425). Implementations live in ViewSetProxyBase.
 *
 *   class ItemViewSet extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin, LookupMixin]) {}
 */

/*
 * Declaration merging is intended: ViewSetProxyBase implements the members. The class halves' type parameters
 * are unused but must match their interfaces'.
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

export interface ListMixin<T> {
  list(params?: ListParams): ViewSetActionCall<T[]>;
}
export class ListMixin<T> {
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

/** Extra member of a ViewSet declaring a list action; it reads the BE schema rather than an endpoint. */
export interface ListDefaultSort {
  /**
   * The BE viewset's `default_sort` (`[]` when none), read from the `sort` parameter default in its `/schema`.
   * Rejects when the schema cannot be fetched; the next call fetches again.
   */
  defaultSort(): Promise<SortState>;
}

/** The actions that read the list endpoint. */
export type ListAction = 'list' | 'listPage' | 'listCursor';

/** `ListDefaultSort` when the member names `A` include a list action, `unknown` otherwise. */
export type DefaultSortOf<A> = [Extract<A, ListAction>] extends [never] ? unknown : ListDefaultSort;

/** One page, mirroring the BE PaginatedList. `count` is null when the backend cannot know it cheaply. */
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
 * One cursor page; no total count.
 *
 * `next`/`previous` are exclusive. `first`/`last` are the same edges read inclusively: they repeat their own
 * row but survive inserts at that edge (for polling a live list); present whenever the page is non-empty.
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
  listCursor(params?: CursorParams): ViewSetActionCall<CursorPage<T>>;
}
export class CursorListMixin<T> {
  static readonly actions: readonly string[] = ['listCursor'];
}

/**
 * FE counterpart of the BE PaginatedListMixin. `list()`, `listPage()` and `listCursor()` read the same
 * `GET {basePath}` and each requests its shape with `X-List-Shape`, so a ViewSet declares one mixin
 * per shape the BE viewset offers through `list_shape` and `list_shapes`.
 */
export interface PaginatedListMixin<T> {
  listPage(params?: PageParams): ViewSetActionCall<PaginatedList<T>>;
}
export class PaginatedListMixin<T> {
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
 * The actions named by `A`, each with its mixin's signature. Composites are absent so each signature exists
 * once; an intersection rather than `Pick<>` keeps the members overridable methods (TS2425).
 */
export type ActionSurface<K extends KeyType, T, PK extends keyof T, A, L = LookupItem> = ('create' extends A
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
  ('lookup' extends A ? LookupMixin<L> : unknown);

/** Every action name there is: `A = string` selects all of them. */
export type ActionName = keyof ActionSurface<KeyType, unknown, never, string>;
