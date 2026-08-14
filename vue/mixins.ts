/**
 * FE counterpart of BE mixins.py — the mixins a ViewSet declaration is composed of.
 *
 * Each mixin corresponds to its BE counterpart and names the actions that one contributes:
 *
 *   class ItemViewSet extends RestProxyImpl<number, Item, 'id'> {
 *     static declares = [BulkViewSetMixin, LookupMixin];
 *   }
 *
 * The `declares` list is what the startup schema check reads. It exists because a TypeScript
 * `implements` clause cannot: it is erased before anything runs, and on these classes it does not
 * even narrow the type, since the proxy base implements every action and therefore satisfies any
 * subset trivially. So `declares` replaces the `implements` clause rather than repeating it.
 *
 * `actions` on a composite mixin is spread from the mixins it is composed of, so the list and the
 * class hierarchy cannot drift apart.
 *
 * The methods stay `declare`-only: the implementation is one HTTP call in ViewSetProxyBase, and a
 * mixin that carried its own would be a second copy of it.
 */

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

export class CreateMixin<T, PK extends keyof T> {
  static readonly actions: readonly string[] = ['create'];

  declare create: (data: Omit<T, PK>) => Promise<T>;
}

export class BulkOnlyCreateMixin<T, PK extends keyof T> {
  static readonly actions: readonly string[] = ['bulkCreate'];

  declare bulkCreate: (data: Omit<T, PK>[]) => Promise<T[]>;
}

export class BulkCreateMixin<T, PK extends keyof T> extends CreateMixin<T, PK> implements BulkOnlyCreateMixin<T, PK> {
  static readonly actions: readonly string[] = [...CreateMixin.actions, ...BulkOnlyCreateMixin.actions];

  declare bulkCreate: (data: Omit<T, PK>[]) => Promise<T[]>;
}

export class ListMixin<T> {
  static readonly actions: readonly string[] = ['list'];

  declare list: (params?: ListParams) => Promise<T[]>;
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

/** FE counterpart of the BE CursorListMixin. A ViewSet declares this or PaginatedListMixin. */
export class CursorListMixin<T> {
  static readonly actions: readonly string[] = ['listCursor'];

  declare listCursor: (params?: CursorParams) => Promise<CursorPage<T>>;
}

/**
 * FE counterpart of the BE PaginatedListMixin. A ViewSet declares either this or ListMixin: the BE
 * endpoint answers with one shape or the other, decided per viewset, never per request.
 */
export class PaginatedListMixin<T> {
  static readonly actions: readonly string[] = ['listPage'];

  declare listPage: (params?: PageParams) => Promise<PaginatedList<T>>;
}

export class RetrieveMixin<K extends KeyType, T> {
  static readonly actions: readonly string[] = ['retrieve'];

  declare retrieve: (pk: K) => Promise<T>;
}

export class UpdateMixin<K extends KeyType, T> {
  static readonly actions: readonly string[] = ['update', 'partialUpdate'];

  declare update: (pk: K, data: T) => Promise<T>;
  declare partialUpdate: (pk: K, data: Partial<T>) => Promise<T>;
}

export class BulkOnlyUpdateMixin<K extends KeyType, T> {
  static readonly actions: readonly string[] = ['bulkUpdate', 'bulkPartialUpdate'];

  declare bulkUpdate: (records: Record<K, T>) => Promise<T[]>;
  declare bulkPartialUpdate: (records: Record<K, Partial<T>>) => Promise<T[]>;
}

export class BulkUpdateMixin<K extends KeyType, T> extends UpdateMixin<K, T> implements BulkOnlyUpdateMixin<K, T> {
  static readonly actions: readonly string[] = [...UpdateMixin.actions, ...BulkOnlyUpdateMixin.actions];

  declare bulkUpdate: (records: Record<K, T>) => Promise<T[]>;
  declare bulkPartialUpdate: (records: Record<K, Partial<T>>) => Promise<T[]>;
}

export class DestroyMixin<K extends KeyType> {
  static readonly actions: readonly string[] = ['destroy'];

  declare destroy: (pk: K) => Promise<DestroyReturnData>;
}

export class BulkOnlyDestroyMixin<K extends KeyType> {
  static readonly actions: readonly string[] = ['bulkDestroy'];

  declare bulkDestroy: (pks: K[]) => Promise<DestroyReturnData[]>;
}

export class BulkDestroyMixin<K extends KeyType> extends DestroyMixin<K> implements BulkOnlyDestroyMixin<K> {
  static readonly actions: readonly string[] = [...DestroyMixin.actions, ...BulkOnlyDestroyMixin.actions];

  declare bulkDestroy: (pks: K[]) => Promise<DestroyReturnData[]>;
}

export class LookupMixin {
  static readonly actions: readonly string[] = ['lookup'];

  declare lookup: () => Promise<LookupItem[]>;
}

export class ReadOnlyViewSetMixin<K extends KeyType, T> extends ListMixin<T> implements RetrieveMixin<K, T> {
  static readonly actions: readonly string[] = [...ListMixin.actions, ...RetrieveMixin.actions];

  declare retrieve: (pk: K) => Promise<T>;
}

export class ViewSetMixin<K extends KeyType, T, PK extends keyof T>
  extends ReadOnlyViewSetMixin<K, T>
  implements CreateMixin<T, PK>, UpdateMixin<K, T>, DestroyMixin<K>
{
  static readonly actions: readonly string[] = [
    ...ReadOnlyViewSetMixin.actions,
    ...CreateMixin.actions,
    ...UpdateMixin.actions,
    ...DestroyMixin.actions,
  ];

  declare create: (data: Omit<T, PK>) => Promise<T>;
  declare update: (pk: K, data: T) => Promise<T>;
  declare partialUpdate: (pk: K, data: Partial<T>) => Promise<T>;
  declare destroy: (pk: K) => Promise<Record<string, unknown>>;
}

export class BulkViewSetMixin<K extends KeyType, T, PK extends keyof T>
  extends ViewSetMixin<K, T, PK>
  implements BulkCreateMixin<T, PK>, BulkUpdateMixin<K, T>, BulkDestroyMixin<K>
{
  static readonly actions: readonly string[] = [
    ...ViewSetMixin.actions,
    ...BulkOnlyCreateMixin.actions,
    ...BulkOnlyUpdateMixin.actions,
    ...BulkOnlyDestroyMixin.actions,
  ];

  declare bulkCreate: (data: Omit<T, PK>[]) => Promise<T[]>;
  declare bulkUpdate: (records: Record<K, T>) => Promise<T[]>;
  declare bulkPartialUpdate: (records: Record<K, Partial<T>>) => Promise<T[]>;
  declare bulkDestroy: (pks: K[]) => Promise<Record<string, unknown>[]>;
}
