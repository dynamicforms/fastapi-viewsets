# Vue Mixins — API Reference

```ts
import { ... } from '@dynamicforms/fastapi-viewsets';
```

Each mixin is an interface merged with a class of the same name. The interface names the actions and their signatures; the class carries `static readonly actions`, the same statement in a form that survives to runtime. A ViewSet declaration lists the mixin classes themselves: the list types the ViewSet's public surface, and is what the schema check compares against the BE.

The members are methods, not function-valued properties, so a ViewSet may override an action with a method of its own.

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

/** `sort` is 'column:asc,other:desc'; the rest are filters. */
interface ListParams {
  sort?: string;
  [key: string]: string | number | boolean | null | undefined | Array<string | number>;
}

interface PageParams extends ListParams {
  offset?: number;
  limit?: number;
}

interface PaginatedList<T> {
  results: T[];
  offset: number;
  limit: number | null;
  count: number | null;
  hasMore: boolean;
  hasPrevious: boolean;
}

interface CursorParams extends ListParams {
  cursor?: string;
  limit?: number;
}

interface CursorPage<T> {
  results: T[];
  limit: number;
  hasMore: boolean;
  hasPrevious: boolean;
  next: string | null;
  previous: string | null;
  first: string | null;
  last: string | null;
}
```

## Individual operation mixins

### CreateMixin `<T, PK extends keyof T>`
```ts
create(data: Omit<T, PK>): Promise<T>;
```

### BulkOnlyCreateMixin `<T, PK extends keyof T>`
```ts
bulkCreate(data: Omit<T, PK>[]): Promise<T[]>;
```

### BulkCreateMixin `<T, PK extends keyof T>`
Interface extends `CreateMixin<T, PK>` and `BulkOnlyCreateMixin<T, PK>`; `actions` spreads both.

### ListMixin `<T>`
```ts
list(params?: ListParams): Promise<T[]>;
```

### PaginatedListMixin `<T>`
```ts
listPage(params?: PageParams): Promise<PaginatedList<T>>;
```

### CursorListMixin `<T>`
```ts
listCursor(params?: CursorParams): Promise<CursorPage<T>>;
```

`GET {basePath}` is one endpoint that answers in whichever shape the BE viewset declared as its default. This client sends no `X-List-Shape` header, so declare the mixin matching that default - declaring several does not let you choose between them.

### RetrieveMixin `<K extends KeyType, T>`
```ts
retrieve(pk: K): Promise<T>;
```

### UpdateMixin `<K extends KeyType, T>`
```ts
update(pk: K, data: T): Promise<T>;
partialUpdate(pk: K, data: Partial<T>): Promise<T>;
```

### BulkOnlyUpdateMixin `<K extends KeyType, T>`
```ts
bulkUpdate(records: Record<K, T>): Promise<T[]>;
bulkPartialUpdate(records: Record<K, Partial<T>>): Promise<T[]>;
```

### BulkUpdateMixin `<K extends KeyType, T>`
Interface extends `UpdateMixin<K, T>` and `BulkOnlyUpdateMixin<K, T>`; `actions` spreads both.

### DestroyMixin `<K extends KeyType>`
```ts
destroy(pk: K): Promise<DestroyReturnData>;
```

### BulkOnlyDestroyMixin `<K extends KeyType>`
```ts
bulkDestroy(pks: K[]): Promise<DestroyReturnData[]>;
```

### BulkDestroyMixin `<K extends KeyType>`
Interface extends `DestroyMixin<K>` and `BulkOnlyDestroyMixin<K>`; `actions` spreads both.

### LookupMixin
```ts
lookup(): Promise<LookupItem[]>;
```

## Combined viewset mixins

A composite restates no signature: its interface extends the leaves its `actions` names, so each action is written in exactly one place.

### ReadOnlyViewSetMixin `<K extends KeyType, T>`
Interface extends `ListMixin<T>` and `RetrieveMixin<K, T>`; `actions` spreads both.

### ViewSetMixin `<K extends KeyType, T, PK extends keyof T>`
Interface extends `ReadOnlyViewSetMixin<K, T>`, `CreateMixin<T, PK>`, `UpdateMixin<K, T>` and `DestroyMixin<K>`; `actions` spreads all four.

### BulkViewSetMixin `<K extends KeyType, T, PK extends keyof T>`
Interface extends `ViewSetMixin<K, T, PK>`, `BulkOnlyCreateMixin<T, PK>`, `BulkOnlyUpdateMixin<K, T>` and `BulkOnlyDestroyMixin<K>`; `actions` spreads all four.
