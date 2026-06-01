# Vue Mixins — API Reference

```ts
import { ... } from '@dynamicforms/fastapi-viewsets';
```

## Types

```ts
type KeyType = string | number;
type DestroyReturnData = Record<KeyType, any>;

interface LookupItem {
  group: unknown;
  pk: unknown;
  title: string;
  icon: string | null;
}
```

## Individual operation mixins

### CreateMixin `<T, PK extends keyof T>`
```ts
declare create: (data: Omit<T, PK>) => Promise<T>;
```

### BulkOnlyCreateMixin `<T, PK extends keyof T>`
```ts
declare bulkCreate: (data: Omit<T, PK>[]) => Promise<T[]>;
```

### BulkCreateMixin `<T, PK>`
Extends `CreateMixin`, implements `BulkOnlyCreateMixin`.

### ListMixin `<T>`
```ts
declare list: () => Promise<T[]>;
```

### RetrieveMixin `<K extends KeyType, T>`
```ts
declare retrieve: (pk: K) => Promise<T>;
```

### UpdateMixin `<K extends KeyType, T>`
```ts
declare update: (pk: K, data: T) => Promise<T>;
declare partialUpdate: (pk: K, data: Partial<T>) => Promise<T>;
```

### BulkOnlyUpdateMixin `<K extends KeyType, T>`
```ts
declare bulkUpdate: (records: Record<K, T>) => Promise<T[]>;
declare bulkPartialUpdate: (records: Record<K, Partial<T>>) => Promise<T[]>;
```

### BulkUpdateMixin `<K extends KeyType, T>`
Extends `UpdateMixin`, implements `BulkOnlyUpdateMixin`.

### DestroyMixin `<K extends KeyType>`
```ts
declare destroy: (pk: K) => Promise<DestroyReturnData>;
```

### BulkOnlyDestroyMixin `<K extends KeyType>`
```ts
declare bulkDestroy: (pks: K[]) => Promise<DestroyReturnData[]>;
```

### BulkDestroyMixin `<K extends KeyType>`
Extends `DestroyMixin`, implements `BulkOnlyDestroyMixin`.

### LookupMixin
```ts
declare lookup: () => Promise<LookupItem[]>;
```

## Combined viewset mixins

### ReadOnlyViewSetMixin `<K, T>`
Extends `ListMixin<T>`, implements `RetrieveMixin<K, T>`.

### ViewSetMixin `<K, T, PK>`
Extends `ReadOnlyViewSetMixin<K, T>`, implements `CreateMixin<T, PK>`, `UpdateMixin<K, T>`, `DestroyMixin<K>`.

### BulkViewSetMixin `<K, T, PK>`
Extends `ViewSetMixin<K, T, PK>`, implements `BulkCreateMixin<T, PK>`, `BulkUpdateMixin<K, T>`, `BulkDestroyMixin<K>`.
