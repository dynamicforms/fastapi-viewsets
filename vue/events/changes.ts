/**
 * `records_changed`, the event the BE's `ChangeEvents` publishes to a viewset's topic after every
 * create, update and destroy action, and what a ViewSet does with it: `watch()` and `applyChanges()`.
 */

import type { CursorPage, KeyType, PaginatedList } from '../mixins';

import {
  defineEvent,
  deferSubscription,
  type EventClient,
  type EventMeta,
  type EventSubscription,
  rethrowLater,
} from './client';

/** One changed record: its pk, and the record `watch({ fetch: true })` retrieved for it. */
export interface Change<T, K extends KeyType = KeyType> {
  pk: K;
  /**
   * The record `retrieve(pk)` answered after the change, in the endpoint's format. The BE sends the pk
   * only; `record` is set by `watch({ fetch: true })` and absent otherwise.
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
 * The options of `watch()`. The callbacks are function-typed properties, so a callback whose
 * parameter type is not the one below does not compile.
 */
export interface WatchOptions<T, K extends KeyType = KeyType> {
  /**
   * Called once per `records_changed` event of version 1 of the ViewSet's topic, in rev order, with
   * the change and the event's `EventMeta`; with `fetch`, twice for an event some of whose retrieves
   * answer 404. An event of any other version calls `onResync` instead.
   */
  onChange: (change: RecordsChanged<T, K>, meta: EventMeta) => void;
  /**
   * Called on a resync of the topic, and for a `records_changed` event whose version is not 1: the
   * caller re-reads what it displays.
   */
  onResync: () => void;
  /**
   * `true` completes every `created` and `updated` change without `record` with `retrieve(pk)`
   * before `onChange`. The changes whose retrieve answers 404 leave the event and follow it in a
   * second `onChange` with `op: 'deleted'` and the same `meta`, in their order in the event; an event
   * whose every retrieve answers 404 calls `onChange` once, with `op: 'deleted'`. Default `false`.
   */
  fetch?: boolean;
  /**
   * Called with the error of a refused `subscribe` or `poll` of the topic, and with the error of a
   * `retrieve` that `fetch` sent and that failed with anything but a 404; `onResync()` follows the latter.
   */
  onError?: (error: unknown) => void;
}

/** `watch()`, which every ViewSet has. */
export interface ViewSetWatch<T, K extends KeyType = KeyType> {
  /**
   * Subscribes the ViewSet's `basePath` to `records_changed` and returns the subscription. Uses the
   * `events` client of the ViewSet's options; without one, a muxws ViewSet uses the client
   * registered for its peer (`eventsOf`), and a REST ViewSet throws.
   */
  watch(options: WatchOptions<T, K>): EventSubscription;
}

/** The record type a mixin interface `M` names, read off its actions; `Record<string, unknown>` for none. */
export type RecordOf<M> = M extends { retrieve(...args: any[]): Promise<infer T> }
  ? T
  : M extends { list(...args: any[]): Promise<(infer T)[]> }
    ? T
    : M extends { create(...args: any[]): Promise<infer T> }
      ? T
      : M extends { listPage(...args: any[]): Promise<PaginatedList<infer T>> }
        ? T
        : M extends { listCursor(...args: any[]): Promise<CursorPage<infer T>> }
          ? T
          : Record<string, unknown>;

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
  retrieve(pk: K): Promise<T>;
}

/**
 * `watch()` of a ViewSet: subscribes `target.topic` to `records_changed` on `client`, or on the
 * client `client` resolves to.
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

  /** The events `change` is passed to `onChange` as, or `null` for a failed retrieve. */
  const complete = async (change: RecordsChanged<T, K>): Promise<RecordsChanged<T, K>[] | null> => {
    if (!options.fetch || change.op === 'deleted') return [change];
    let failed: unknown = undefined;
    const gone = Symbol('gone');
    const changes = await Promise.all(
      change.changes.map(async (item): Promise<Change<T, K> | typeof gone | null> => {
        if (item.record !== undefined && item.record !== null) return item;
        try {
          return { pk: item.pk, record: await target.retrieve(item.pk) };
        } catch (error) {
          if ((error as { response?: { status?: unknown } } | null)?.response?.status === 404) return gone;
          failed ??= error;
          return null;
        }
      }),
    );
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
