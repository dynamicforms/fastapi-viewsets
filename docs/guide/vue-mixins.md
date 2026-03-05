# Vue / TypeScript Mixins

The `@dynamicforms/viewsets` package ships TypeScript mixin classes that mirror the Python backend mixins. They serve as type declarations — you use them to tell TypeScript which operations your viewset supports, and `route_rest` provides the actual HTTP implementation.

## Individual operation mixins

| Class | Methods declared |
|-------|-----------------|
| `CreateMixin<T, PK>` | `create(data)` |
| `BulkOnlyCreateMixin<T, PK>` | `bulkCreate(data[])` |
| `BulkCreateMixin<T, PK>` | `create`, `bulkCreate` |
| `ListMixin<T>` | `list()` |
| `RetrieveMixin<K, T>` | `retrieve(pk)` |
| `UpdateMixin<K, T>` | `update(pk, data)`, `partialUpdate(pk, data)` |
| `BulkOnlyUpdateMixin<K, T>` | `bulkUpdate(records)`, `bulkPartialUpdate(records)` |
| `BulkUpdateMixin<K, T>` | all four update methods |
| `DestroyMixin<K>` | `destroy(pk)` |
| `BulkOnlyDestroyMixin<K>` | `bulkDestroy(pks[])` |
| `BulkDestroyMixin<K>` | `destroy`, `bulkDestroy` |
| `LookupMixin` | `lookup()` |

## Combined viewset mixins

| Class | Extends |
|-------|---------|
| `ReadOnlyViewSetMixin<K, T>` | `ListMixin`, `RetrieveMixin` |
| `ViewSetMixin<K, T, PK>` | `ReadOnlyViewSetMixin` + `CreateMixin`, `UpdateMixin`, `DestroyMixin` |
| `BulkViewSetMixin<K, T, PK>` | `ViewSetMixin` + `BulkCreateMixin`, `BulkUpdateMixin`, `BulkDestroyMixin` |

## Type parameters

| Parameter | Description |
|-----------|-------------|
| `K` | Primary key type — `number` or `string` |
| `T` | Model interface |
| `PK` | Key of the PK field on `T` (e.g. `'id'`) — used to omit it from create payloads |

## LookupItem

The `lookup()` method returns `LookupItem[]`:

```ts
interface LookupItem {
  group: unknown;
  pk: unknown;
  title: string;
  icon: string | null;
}
```

## Declaring a viewset class

Declare a class that extends the appropriate mixin. The class body is empty — it is only used as a type token for `route_rest`:

```ts
import { BulkViewSetMixin, LookupMixin } from '@dynamicforms/viewsets';

interface Item {
  id: number;
  name: string;
  price: number;
}

class ItemViewSet extends BulkViewSetMixin<number, Item, 'id'> implements LookupMixin {}
```

Then pass it to `route_rest`:

```ts
import { route_rest } from '@dynamicforms/viewsets';

const itemsApi = route_rest<BulkViewSetMixin<number, Item, 'id'> & LookupMixin>(
  ItemViewSet,
  '/items',
  'id',
);
```

See [route_rest](./route-rest) for full usage details.
