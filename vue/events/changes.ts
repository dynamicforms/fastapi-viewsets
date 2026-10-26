/**
 * `records_changed`, the event the BE's `ChangeEvents` publishes to a viewset's topic after every
 * create, update and destroy action, and what a ViewSet does with it: `watch()` and `applyChanges()`.
 */

import { inBackground } from '../call';
import type { CursorPage, KeyType, ListRowsOf, PaginatedList } from '../mixins';

import {
  defineEvent,
  deferSubscription,
  type EventClient,
  type EventMeta,
  type EventSubscription,
  rethrowLater,
} from './client';

/** One changed record: its pk, and the row `watch()` with `fetch` read for it. */
export interface Change<T, K extends KeyType = KeyType> {
  pk: K;
  /**
   * The row `fetch` read after the change: what `retrieve(pk)` answered for `fetch: true`, the row a
   * `fetch` function returned for the pk otherwise. The BE sends the pk only; `record` is absent
   * without `fetch`.
   */
  record?: T | null;
}

/** The payload of `records_changed`, version 1: one action's changes. */
export interface RecordsChanged<T, K extends KeyType = KeyType> {
  op: 'created' | 'updated' | 'deleted';
  changes: Change<T, K>[];
}

/** The `records_changed` event. The BE's `RecordsChanged` is version 1. */
export const RECORDS_CHANGED = defineEvent<RecordsChanged<unknown>>('records_changed');

/**
 * `WatchOptions.fetch` as a function: the rows of `pks`, in any order. `watch()` calls it once per
 * `created` or `updated` event, with the pks of the changes that carry no `record`, and matches the
 * rows it returns to them by the ViewSet's `pkFieldName`; a pk without a row is a deleted one.
 *
 *   fetch: (pks) => members.list({ ids: pks })
 */
export type FetchRows<T, K extends KeyType = KeyType> = (pks: K[]) => Promise<readonly T[]>;

/** `true` when `A` and `B` are the same type. */
type Same<A, B> = (<G>() => G extends A ? 1 : 2) extends <G>() => G extends B ? 1 : 2 ? true : false;

/**
 * The `fetch` option of `watch()` on a ViewSet whose list rows, without `listType`, are `R` and
 * whose records are `T`: `boolean | FetchRows<R, K>` when `R` is `T`, `false | FetchRows<R, K>`
 * otherwise, because `fetch: true` reads records with `retrieve`.
 */
export type FetchOption<R, T, K extends KeyType = KeyType> =
  Same<R, T> extends true ? boolean | FetchRows<R, K> : false | FetchRows<R, K>;

/**
 * The options of `watch()`. The callbacks are function-typed properties, so a callback whose
 * parameter type is not the one below does not compile.
 */
export interface WatchOptions<T, K extends KeyType = KeyType> {
  /**
   * Called once per `records_changed` event of version 1 of the ViewSet's topic, in rev order, with
   * the change and the event's `EventMeta`; with `fetch`, twice for an event some of whose records
   * are gone. An event of any other version calls `onResync` instead.
   */
  onChange: (change: RecordsChanged<T, K>, meta: EventMeta) => void;
  /**
   * Called on a resync of the topic, and for a `records_changed` event whose version is not 1: the
   * caller re-reads what it displays.
   */
  onResync: () => void;
  /**
   * Completes every `created` and `updated` change without `record` before `onChange`:
   *
   * - `true` with `retrieve(pk)`, one call per change; a 404 means the record is gone.
   * - a `FetchRows` function with the rows it returns for the event's pks, one call per event; a pk
   *   without a row is gone. A ViewSet whose list rows are not its records (`R` of
   *   `restViewSet<T, L, V, R>()` other than `T`) takes this form, so that `onChange` receives list
   *   rows.
   *
   * The changes whose record is gone leave the event and follow it in a second `onChange` with
   * `op: 'deleted'` and the same `meta`, in their order in the event; an event whose every record is
   * gone calls `onChange` once, with `op: 'deleted'`. Default `false`.
   */
  fetch?: boolean | FetchRows<T, K>;
  /**
   * Called with the error of a refused `subscribe` or `poll` of the topic, with the error of a
   * `retrieve` that `fetch: true` sent and that failed with anything but a 404, and with the error of
   * a `fetch` function; `onResync()` follows the latter two.
   */
  onError?: (error: unknown) => void;
  /**
   * The client topic to subscribe: the BE path of the viewset relative to the application's
   * `root_path`, without a Starlette `Mount` prefix. Default `ProxyBaseOptions.topic`, else the
   * ViewSet's `basePath`.
   */
  topic?: string;
}

/**
 * `watch()`, which every ViewSet has. `T` is the type of the rows `onChange` receives without a
 * `fetch` function, the row type of the ViewSet's list without `listType`; `F` the `fetch` option
 * it accepts (`FetchOption`). With a `fetch` function, `onChange` receives the rows that function
 * returns, so a list type selected with `listType` is watched by fetching with the same `listType`:
 *
 *   api.watch({ fetch: (pks) => api.list({ id__in: pks }, { listType: 'compact' }), onChange, onResync })
 */
export interface ViewSetWatch<T, K extends KeyType = KeyType, F = boolean | FetchRows<T, K>> {
  /**
   * Subscribes the ViewSet's topic (`WatchOptions.topic`, else `ProxyBaseOptions.topic`, else
   * `basePath`) to `records_changed` and returns the subscription. Uses the
   * `events` client of the ViewSet's options; without one, a muxws ViewSet uses the client
   * registered for its peer (`eventsOf`), and a REST ViewSet throws.
   */
  watch<Row>(options: Omit<WatchOptions<NoInfer<Row>, K>, 'fetch'> & { fetch: FetchRows<Row, K> }): EventSubscription;
  watch(options: WatchOptions<T, K> & { fetch?: F }): EventSubscription;
}

/** The record type a mixin interface `M` names, read off its actions; `Record<string, unknown>` for none. */
export type RecordOf<M> = M extends { retrieve(...args: any[]): Promise<infer T> }
  ? T
  : [ListRowsOf<M>] extends [never]
    ? RecordFromActions<M>
    : ListRowsOf<M> extends { row: infer T }
      ? T
      : RecordFromActions<M>;

/** `RecordOf` of a mixin interface without `ListRows`, read off the return types of its actions. */
type RecordFromActions<M> = M extends { list(...args: any[]): Promise<(infer T)[]> }
  ? T
  : M extends { create(...args: any[]): Promise<infer T> }
    ? T
    : M extends { listPage(...args: any[]): Promise<PaginatedList<infer T>> }
      ? T
      : M extends { listCursor(...args: any[]): Promise<CursorPage<infer T>> }
        ? T
        : Record<string, unknown>;

/**
 * The row type a list call of a mixin interface `M` answers with when it names no `listType`:
 * the row type of its list mixin (`ListRows`), else `RecordOf<M>`.
 */
export type RowOf<M> = [ListRowsOf<M>] extends [never]
  ? RecordOf<M>
  : ListRowsOf<M> extends { row: infer R }
    ? R
    : RecordOf<M>;

/** The pk type a mixin interface `M` names, read off its actions; `KeyType` for none. */
export type KeyOf<M> = M extends { retrieve(pk: infer K): any }
  ? K & KeyType
  : M extends { destroy(pk: infer K): any }
    ? K & KeyType
    : M extends { update(pk: infer K, ...args: any[]): any }
      ? K & KeyType
      : KeyType;

/**
 * `rows` with `change` applied, as a new array; `rows` is not modified.
 *
 * - `deleted` removes every row whose pk is a changed pk.
 * - `created` and `updated` replace every row with the change's pk by the change's `record`, or
 *   append the record when no row has that pk. A change without `record` is skipped.
 *
 * Pks compare by their string form, so `7` and `'7'` name one row.
 */
export function applyChanges<T, K extends KeyType = KeyType>(
  rows: readonly T[],
  change: RecordsChanged<T, K>,
  pkFieldName: string,
): T[] {
  const pkOf = (row: T): string => String((row as Record<string, unknown>)[pkFieldName]);
  if (change.op === 'deleted') {
    const removed = new Set(change.changes.map((item) => String(item.pk)));
    return rows.filter((row) => !removed.has(pkOf(row)));
  }

  const records = new Map<string, T>();
  for (const item of change.changes) {
    if (item.record !== undefined && item.record !== null) records.set(String(item.pk), item.record);
  }
  const placed = new Set<string>();
  const out = rows.map((row) => {
    const pk = pkOf(row);
    const record = records.get(pk);
    if (record === undefined) return row;
    placed.add(pk);
    return record;
  });
  for (const [pk, record] of records) if (!placed.has(pk)) out.push(record);
  return out;
}

/** What `watchRecords` needs of a ViewSet. */
export interface WatchTarget<T, K extends KeyType> {
  readonly topic: string;
  /** The field a `FetchRows` row carries its pk in. */
  readonly pkFieldName: string;
  retrieve(pk: K): Promise<T>;
}

/**
 * `watch()` of a ViewSet: subscribes `target.topic` to `records_changed` on `client`, or on the
 * client `client` resolves to. The calls of `fetch` are issued inside `inBackground`, so call
 * activity reports them as background calls.
 *
 * `onChange` and `onResync` are called in the order their events arrived, also while `fetch` is
 * waiting for a `retrieve`. An event whose version is not 1 has a payload `RecordsChanged` does not
 * describe; it calls `onResync`, with or without `fetch`. After `unsubscribe()` of the returned
 * subscription no option callback is called, also for an event that arrived before it.
 */
export function watchRecords<T, K extends KeyType>(
  target: WatchTarget<T, K>,
  client: EventClient | Promise<EventClient>,
  options: WatchOptions<T, K>,
): EventSubscription {
  let stopped = false;
  let queue: Promise<void> = Promise.resolve();
  const enqueue = (step: () => void | Promise<void>): void => {
    queue = queue.then(() => (stopped ? undefined : step())).catch(rethrowLater);
  };

  const gone = Symbol('gone');
  const hasRecord = (item: Change<T, K>): boolean => item.record !== undefined && item.record !== null;

  /** `fetch: true`: each change without a record completed with `retrieve`. */
  const retrieveEach = async (
    items: Change<T, K>[],
  ): Promise<{ changes: (Change<T, K> | typeof gone | null)[]; failed: unknown }> => {
    let failed: unknown = undefined;
    const changes = await Promise.all(
      items.map(async (item): Promise<Change<T, K> | typeof gone | null> => {
        if (hasRecord(item)) return item;
        try {
          return { pk: item.pk, record: await inBackground(() => target.retrieve(item.pk)) };
        } catch (error) {
          if ((error as { response?: { status?: unknown } } | null)?.response?.status === 404) return gone;
          failed ??= error;
          return null;
        }
      }),
    );
    return { changes, failed };
  };

  /** A `FetchRows` function: the changes without a record completed by one call. */
  const fetchRows = async (
    items: Change<T, K>[],
    fetch: FetchRows<T, K>,
  ): Promise<{ changes: (Change<T, K> | typeof gone | null)[]; failed: unknown }> => {
    const pks = items.filter((item) => !hasRecord(item)).map((item) => item.pk);
    if (pks.length === 0) return { changes: items, failed: undefined };
    let rows: readonly T[];
    try {
      rows = await inBackground(() => fetch(pks));
    } catch (error) {
      return { changes: [], failed: error ?? new Error('fetch rejected') };
    }
    const byPk = new Map<string, T>();
    for (const row of rows) byPk.set(String((row as Record<string, unknown>)[target.pkFieldName]), row);
    return {
      changes: items.map((item) => {
        if (hasRecord(item)) return item;
        const record = byPk.get(String(item.pk));
        return record === undefined ? gone : { pk: item.pk, record };
      }),
      failed: undefined,
    };
  };

  /** The events `change` is passed to `onChange` as, or `null` for a failed fetch. */
  const complete = async (change: RecordsChanged<T, K>): Promise<RecordsChanged<T, K>[] | null> => {
    if (!options.fetch || change.op === 'deleted') return [change];
    const { changes, failed } =
      typeof options.fetch === 'function'
        ? await fetchRows(change.changes, options.fetch)
        : await retrieveEach(change.changes);
    if (failed !== undefined) {
      const error = failed;
      if (options.onError && !stopped) options.onError(error);
      return null;
    }
    const found = changes.filter((item): item is Change<T, K> => item !== gone && item !== null);
    const deleted = change.changes.filter((_, index) => changes[index] === gone).map(({ pk }) => ({ pk }));
    const out: RecordsChanged<T, K>[] = [];
    if (found.length > 0 || deleted.length === 0) out.push({ op: change.op, changes: found });
    if (deleted.length > 0) out.push({ op: 'deleted', changes: deleted });
    return out;
  };

  const subscribe = (events: EventClient): EventSubscription =>
    events.subscribe(target.topic, RECORDS_CHANGED, {
      onEvent(payload, meta) {
        if (meta.version !== 1) {
          enqueue(() => options.onResync());
          return;
        }
        const change = payload as RecordsChanged<T, K>;
        enqueue(async () => {
          const completed = await complete(change);
          if (stopped) return;
          if (!completed) {
            options.onResync();
            return;
          }
          for (const item of completed) {
            if (stopped) return;
            options.onChange(item, meta);
          }
        });
      },
      onResync() {
        enqueue(() => options.onResync());
      },
      onError: options.onError ? (error) => options.onError?.(error) : undefined,
    });

  const subscription =
    client instanceof Promise
      ? deferSubscription(client, subscribe, options.onError?.bind(options))
      : subscribe(client);
  return {
    get ready() {
      return subscription.ready;
    },
    unsubscribe() {
      stopped = true;
      subscription.unsubscribe();
    },
    get onError() {
      return subscription.onError;
    },
    set onError(handler) {
      subscription.onError = handler;
    },
  };
}
