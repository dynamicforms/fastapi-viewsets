/**
 * FE counterpart of BE mixins.py — abstract mixin classes for ViewSet declarations.
 *
 * Each mixin class corresponds to its BE counterpart. ViewSet classes on the FE
 * declare their capabilities by extending these mixins, mirroring the BE pattern:
 *
 *   class ItemViewSet extends BulkViewSetMixin<number, Item> implements LookupMixin {}
 *
 * The actual HTTP implementation is provided by RestProxyImpl via route_rest().
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
  declare create: (data: Omit<T, PK>) => Promise<T>;
}

export class BulkOnlyCreateMixin<T, PK extends keyof T> {
  declare bulkCreate: (data: Omit<T, PK>[]) => Promise<T[]>;
}

export class BulkCreateMixin<T, PK extends keyof T> extends CreateMixin<T, PK> implements BulkOnlyCreateMixin<T, PK> {
  declare bulkCreate: (data: Omit<T, PK>[]) => Promise<T[]>;
}

export class ListMixin<T> {
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
  declare listCursor: (params?: CursorParams) => Promise<CursorPage<T>>;
}

/**
 * FE counterpart of the BE PaginatedListMixin. A ViewSet declares either this or ListMixin: the BE
 * endpoint answers with one shape or the other, decided per viewset, never per request.
 */
export class PaginatedListMixin<T> {
  declare listPage: (params?: PageParams) => Promise<PaginatedList<T>>;
}

export class RetrieveMixin<K extends KeyType, T> {
  declare retrieve: (pk: K) => Promise<T>;
}

export class UpdateMixin<K extends KeyType, T> {
  declare update: (pk: K, data: T) => Promise<T>;
  declare partialUpdate: (pk: K, data: Partial<T>) => Promise<T>;
}

export class BulkOnlyUpdateMixin<K extends KeyType, T> {
  declare bulkUpdate: (records: Record<K, T>) => Promise<T[]>;
  declare bulkPartialUpdate: (records: Record<K, Partial<T>>) => Promise<T[]>;
}

export class BulkUpdateMixin<K extends KeyType, T> extends UpdateMixin<K, T> implements BulkOnlyUpdateMixin<K, T> {
  declare bulkUpdate: (records: Record<K, T>) => Promise<T[]>;
  declare bulkPartialUpdate: (records: Record<K, Partial<T>>) => Promise<T[]>;
}

export class DestroyMixin<K extends KeyType> {
  declare destroy: (pk: K) => Promise<DestroyReturnData>;
}

export class BulkOnlyDestroyMixin<K extends KeyType> {
  declare bulkDestroy: (pks: K[]) => Promise<DestroyReturnData[]>;
}

export class BulkDestroyMixin<K extends KeyType> extends DestroyMixin<K> implements BulkOnlyDestroyMixin<K> {
  declare bulkDestroy: (pks: K[]) => Promise<DestroyReturnData[]>;
}

export class LookupMixin {
  declare lookup: () => Promise<LookupItem[]>;
}

export class ReadOnlyViewSetMixin<K extends KeyType, T> extends ListMixin<T> implements RetrieveMixin<K, T> {
  declare retrieve: (pk: K) => Promise<T>;
}

export class ViewSetMixin<K extends KeyType, T, PK extends keyof T>
  extends ReadOnlyViewSetMixin<K, T>
  implements CreateMixin<T, PK>, UpdateMixin<K, T>, DestroyMixin<K>
{
  declare create: (data: Omit<T, PK>) => Promise<T>;
  declare update: (pk: K, data: T) => Promise<T>;
  declare partialUpdate: (pk: K, data: Partial<T>) => Promise<T>;
  declare destroy: (pk: K) => Promise<Record<string, unknown>>;
}

export class BulkViewSetMixin<K extends KeyType, T, PK extends keyof T>
  extends ViewSetMixin<K, T, PK>
  implements BulkCreateMixin<T, PK>, BulkUpdateMixin<K, T>, BulkDestroyMixin<K>
{
  declare bulkCreate: (data: Omit<T, PK>[]) => Promise<T[]>;
  declare bulkUpdate: (records: Record<K, T>) => Promise<T[]>;
  declare bulkPartialUpdate: (records: Record<K, Partial<T>>) => Promise<T[]>;
  declare bulkDestroy: (pks: K[]) => Promise<Record<string, unknown>[]>;
}
