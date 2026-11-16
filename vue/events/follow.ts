/**
 * `follow()` of a ViewSet: a list read by the application and kept current with `watch()`. The
 * subscription is made before the first read; a change that arrives while a read is in flight is held
 * and applied after the read; a read that a newer one superseded is dropped.
 */

import { inBackground } from '../call';
import type { KeyType } from '../mixins';

import { applyChanges, type FetchRows, type RecordsChanged, type WatchOptions } from './changes';
import { type EventSubscription, rethrowLater } from './client';

/**
 * The options of `follow()` for rows of any type `R`, e.g. pages with a total. `Row` is the record type
 * the changes carry. The callbacks are function-typed properties, as in `WatchOptions`.
 */
export interface FollowOptions<R, Row, K extends KeyType = KeyType> {
  /** Reads the rows. */
  read: () => Promise<R>;
  /**
   * Returns `rows` with `change` applied, or `'reload'` to read the rows anew. Must return the same rows
   * for a change `rows` already contain: a change that arrives during a read may be in its answer.
   */
  apply: (rows: R, change: RecordsChanged<Row, K>) => R | 'reload';
  /**
   * Called with the rows of the latest read once the held changes are applied, after an `extend()` read
   * merged into them, after each change applied later, and after `apply()`.
   */
  onRows: (rows: R) => void;
  /** Called with the error of a failed read of the latest generation. */
  onLoadError: (error: unknown) => void;
  /** Called with `true` when a read starts while none is in flight, and with `false` once none is in flight. */
  onLoading?: (loading: boolean) => void;
  /** Passed to `watch()`: a refused subscription, a failed `fetch`. */
  onError?: (error: unknown) => void;
  /** Passed to `watch()`. Default `false`. */
  fetch?: boolean | FetchRows<Row, K>;
  /** Passed to `watch()`. */
  topic?: string;
}

/**
 * `FollowOptions` for rows held as an array. `apply` defaults to `applyChanges()` by the ViewSet's
 * `pkFieldName`, and to `'reload'` for a `created` or `updated` change without `record`.
 */
export type FollowRowsOptions<Row, K extends KeyType = KeyType> = Omit<
  FollowOptions<Row[], Row, K>,
  'read' | 'apply'
> & {
  read: () => Promise<Row[]>;
  apply?: FollowOptions<Row[], Row, K>['apply'];
};

/** What `follow()` returns. */
export interface ListFollower<R, Row, K extends KeyType = KeyType> {
  /**
   * Resolves once the first read has settled (rows or a load error) and its held changes are applied, or
   * at `close()`. Never rejects.
   */
  readonly ready: Promise<void>;
  /**
   * The rows of the latest read with the changes applied; `undefined` before it, and after
   * `reload({ reset: true })` until its read lands.
   */
  readonly rows: R | undefined;
  /**
   * Starts a new generation: drops the held changes and the reads in flight, and reads the rows.
   * `reset` sets `rows` to `undefined` until the read lands. Before the subscription is answered, it
   * starts no read and returns the first read's promise. Resolves once the read has settled, or at
   * `close()`. Never rejects.
   */
  reload(options?: { reset?: boolean }): Promise<void>;
  /**
   * A further read of the current generation, e.g. the next page: `merge` combines its value into `rows`
   * when it lands while the generation is current. Does nothing while `rows` is `undefined` and while the
   * read that started the generation is in flight. Never rejects.
   */
  extend<V>(read: () => Promise<V>, merge: (rows: R, value: V) => R): Promise<void>;
  /** Resolves once no read of the current generation is in flight and the held changes are applied. */
  settled(): Promise<void>;
  /**
   * Applies the caller's own change to `rows` at once and calls `onRows`. During a read the change is
   * also held and applied again after the read; while `rows` is `undefined` it is only held then. Does
   * nothing while `rows` is `undefined` and no read is in flight.
   */
  apply(change: RecordsChanged<Row, K>): void;
  /** Unsubscribes and drops the reads in flight and the held changes; no callback is called afterwards. */
  close(): void;
}

/** `follow()`, which every ViewSet has. */
export interface ViewSetFollow<T, K extends KeyType = KeyType> {
  /** Follows rows a `fetch` function returns, read by `read`. */
  follow<Row>(
    options: Omit<FollowRowsOptions<NoInfer<Row>, K>, 'fetch'> & { fetch: FetchRows<Row, K> },
  ): ListFollower<Row[], Row, K>;
  follow<R, Row>(
    options: Omit<FollowOptions<R, NoInfer<Row>, K>, 'fetch'> & { fetch: FetchRows<Row, K> },
  ): ListFollower<R, Row, K>;
  follow(options: FollowRowsOptions<T, K> & { fetch?: boolean }): ListFollower<T[], T, K>;
  follow<R>(options: FollowOptions<R, T, K> & { fetch?: boolean }): ListFollower<R, T, K>;
}

/** `follow()` of a ViewSet with a `list` action: `read` defaults to `list()`. */
export interface FollowList<T, K extends KeyType = KeyType> {
  follow(
    options: Omit<FollowRowsOptions<T, K>, 'read' | 'fetch'> & {
      read?: () => Promise<T[]>;
      fetch?: boolean | FetchRows<T, K>;
    },
  ): ListFollower<T[], T, K>;
}

/** `FollowList` when the member names `A` include `list`, `unknown` otherwise. */
export type FollowListOf<A, T, K extends KeyType> = [Extract<A, 'list'>] extends [never] ? unknown : FollowList<T, K>;

/** What `followRecords` needs of a ViewSet. */
export interface FollowTarget<K extends KeyType> {
  /** The field `applyChanges()` matches rows by. */
  readonly pkFieldName: string;
  watch(options: WatchOptions<any, K>): EventSubscription;
  list(): Promise<unknown[]>;
}

/** The options `followRecords` takes: `read` and `apply` have defaults. */
export type FollowAnyOptions<R, Row, K extends KeyType> = Omit<FollowOptions<R, Row, K>, 'read' | 'apply'> &
  Partial<Pick<FollowOptions<R, Row, K>, 'read' | 'apply'>>;

/** Calls an application callback; what it throws is rethrown by `rethrowLater`. */
function invoke<A>(callback: ((arg: A) => void) | undefined, arg: A): void {
  if (!callback) return;
  try {
    callback(arg);
  } catch (error) {
    rethrowLater(error);
  }
}

/**
 * The default `apply` of `follow()` for rows held as an array: `'reload'` for a `created` or `updated`
 * change with an item without `record`, otherwise `applyChanges(rows, change, pkFieldName)`.
 */
export function applyChangesOrReload<T, K extends KeyType = KeyType>(
  rows: readonly T[],
  change: RecordsChanged<T, K>,
  pkFieldName: string,
): T[] | 'reload' {
  if (change.op !== 'deleted' && change.changes.some((item) => item.record === undefined || item.record === null))
    return 'reload';
  return applyChanges(rows, change, pkFieldName);
}

/** `follow()` of a ViewSet. */
export function followRecords<R, Row, K extends KeyType>(
  target: FollowTarget<K>,
  options: FollowAnyOptions<R, Row, K>,
): ListFollower<R, Row, K> {
  const read = options.read ?? (() => target.list() as Promise<R>);
  const apply =
    options.apply ??
    ((rows: R, change: RecordsChanged<Row, K>) =>
      applyChangesOrReload(rows as Row[], change, target.pkFieldName) as R | 'reload');

  /** Each `reload()` and `close()` starts a new one; a read of an older one is dropped when it lands. */
  let generation = 0;
  /** The reads of the current generation in flight. */
  let inFlight = 0;
  /** The read that started the current generation is in flight; `rows` may be of an older one. */
  let baseInFlight = false;
  /** The changes that arrived while a read of the current generation was in flight, in arrival order. */
  let held: RecordsChanged<Row, K>[] = [];
  let rows: R | undefined;
  /** `rows` changed since `onRows` was last called. */
  let fresh = false;
  let loading = false;
  let phase: 'subscribing' | 'open' | 'closed' = 'subscribing';
  let waiters: (() => void)[] = [];
  /** The resolvers of the `reload()` and `extend()` promises whose read is in flight. */
  const pending = new Set<() => void>();
  /** Settles with the first read, or at `close()`; what `reload()` returns while subscribing. */
  let firstResolve!: (read: Promise<void>) => void;
  const first = new Promise<void>((resolve) => {
    firstResolve = resolve;
  });
  let readyResolve!: () => void;
  const ready = new Promise<void>((resolve) => {
    readyResolve = resolve;
  });

  /** Whether nothing has superseded generation `run`. */
  const current = (run: number): boolean => run === generation && phase !== 'closed';

  const setLoading = (value: boolean): void => {
    if (loading === value) return;
    loading = value;
    invoke(options.onLoading, value);
  };

  /** Applies the held changes, reports the rows and resolves the waiters; runs with no read in flight. */
  const drain = (): void => {
    const run = generation;
    while (held.length > 0 && rows !== undefined) {
      const change = held.shift()!;
      let next: R | 'reload';
      try {
        next = apply(rows, change);
      } catch (error) {
        rethrowLater(error);
        continue;
      }
      if (!current(run)) return;
      if (next === 'reload') {
        void reloadFrom(true);
        return;
      }
      rows = next;
      fresh = true;
    }
    held = [];
    if (fresh && rows !== undefined) {
      fresh = false;
      invoke(options.onRows, rows);
      if (!current(run)) return;
    }
    readyResolve();
    setLoading(false);
    if (!current(run)) return;
    const resolved = waiters;
    waiters = [];
    for (const resolve of resolved) resolve();
  };

  /**
   * Runs `fn` as a read of generation `run`; `land` takes its value while the generation is current.
   * `base` marks the read that started the generation.
   */
  const runRead = <V>(
    run: number,
    fn: () => Promise<V>,
    land: (value: V) => void,
    background: boolean,
    base = false,
  ) => {
    inFlight += 1;
    setLoading(true);
    let promise: Promise<V>;
    try {
      promise = Promise.resolve(background ? inBackground(fn) : fn());
    } catch (error) {
      promise = Promise.reject(error);
    }
    return new Promise<void>((resolve) => {
      pending.add(resolve);
      promise
        .then(
          (value) => {
            if (!current(run)) return;
            try {
              land(value);
            } catch (error) {
              rethrowLater(error);
            }
          },
          (error: unknown) => {
            if (current(run)) invoke(options.onLoadError, error);
          },
        )
        .then(() => {
          pending.delete(resolve);
          if (current(run)) {
            if (base) baseInFlight = false;
            inFlight -= 1;
            if (inFlight === 0) drain();
          }
          resolve();
        });
    });
  };

  const reloadFrom = (background: boolean, reset = false): Promise<void> => {
    if (phase === 'closed') return Promise.resolve();
    phase = 'open';
    generation += 1;
    inFlight = 0;
    held = [];
    baseInFlight = true;
    if (reset) {
      rows = undefined;
      fresh = false;
    }
    return runRead(
      generation,
      read,
      (value) => {
        rows = value;
        fresh = true;
      },
      background,
      true,
    );
  };

  const subscription = target.watch({
    fetch: options.fetch,
    topic: options.topic,
    onError: options.onError
      ? (error) => {
          if (phase !== 'closed') options.onError?.(error);
        }
      : undefined,
    onChange(change: RecordsChanged<Row, K>) {
      // While subscribing, the first read has not started and will contain the change.
      if (phase !== 'open') return;
      if (inFlight > 0) {
        held.push(change);
        return;
      }
      if (rows === undefined) return;
      held.push(change);
      drain();
    },
    onResync() {
      if (phase === 'open') void reloadFrom(true);
    },
  });

  const start = (): void => {
    if (phase === 'subscribing') firstResolve(reloadFrom(false));
  };
  void subscription.ready.then(start, start);

  return {
    ready,
    get rows() {
      return rows;
    },
    reload(reloadOptions?: { reset?: boolean }) {
      // Before the subscription is answered `rows` is `undefined`, and the first read is still to come.
      if (phase === 'subscribing') return first;
      return reloadFrom(false, reloadOptions?.reset === true);
    },
    extend<V>(fn: () => Promise<V>, merge: (rows: R, value: V) => R) {
      if (phase === 'closed' || rows === undefined || baseInFlight) return Promise.resolve();
      return runRead(
        generation,
        fn,
        (value) => {
          rows = merge(rows!, value);
          fresh = true;
        },
        false,
      );
    },
    settled() {
      if (phase === 'closed' || (phase === 'open' && inFlight === 0)) return Promise.resolve();
      return new Promise<void>((resolve) => waiters.push(resolve));
    },
    apply(change: RecordsChanged<Row, K>) {
      if (phase === 'closed') return;
      // Held too, so that the answer of a read in flight does not replace it.
      if (inFlight > 0 && rows === undefined) {
        held.push(change);
        return;
      }
      if (rows === undefined) return;
      const run = generation;
      let next: R | 'reload';
      try {
        next = apply(rows, change);
      } catch (error) {
        rethrowLater(error);
        return;
      }
      if (!current(run)) return;
      if (next === 'reload') {
        void reloadFrom(false);
        return;
      }
      if (inFlight > 0) held.push(change);
      rows = next;
      fresh = false;
      invoke(options.onRows, next);
    },
    close() {
      if (phase === 'closed') return;
      phase = 'closed';
      generation += 1;
      inFlight = 0;
      held = [];
      subscription.unsubscribe();
      firstResolve(Promise.resolve());
      readyResolve();
      const resolved = [...waiters, ...pending];
      waiters = [];
      pending.clear();
      for (const resolve of resolved) resolve();
    },
  };
}
