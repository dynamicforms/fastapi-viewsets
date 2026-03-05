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
  declare list: () => Promise<T[]>;
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
