/**
 * Unit tests for follow.ts: `followRecords` against a fake watch target with a controllable `ready`
 * and deferred reads, and `follow()` on a REST ViewSet.
 */

import type { AxiosInstance } from 'axios';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { type CallEvent, onCallActivity } from '../call';
import { RestProxyImpl } from '../rest-proxy';

import type { RecordsChanged, WatchOptions } from './changes';
import type { EventClient, EventHandlers, EventSubscription } from './client';
import { type FollowAnyOptions, followRecords } from './follow';

interface Item {
  id: number;
  name: string;
}

interface Deferred<V> {
  promise: Promise<V>;
  resolve: (value: V) => void;
  reject: (error: unknown) => void;
}

function deferred<V>(): Deferred<V> {
  let resolve!: (value: V) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<V>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

const item = (id: number, name = `n${id}`): Item => ({ id, name });
const updated = (...records: Item[]): RecordsChanged<Item, number> => ({
  op: 'updated',
  changes: records.map((record) => ({ pk: record.id, record })),
});
const created = (...records: Item[]): RecordsChanged<Item, number> => ({ ...updated(...records), op: 'created' });
const deleted = (...pks: number[]): RecordsChanged<Item, number> => ({
  op: 'deleted',
  changes: pks.map((pk) => ({ pk })),
});

/** A watch target whose subscription answers on `answer()` / `refuse()`, and whose reads answer on demand. */
function harness() {
  const subscribed = deferred<void>();
  const unsubscribe = vi.fn();
  let watchOptions: WatchOptions<Item, number> | undefined;
  const reads: Deferred<Item[]>[] = [];
  const list = vi.fn(() => {
    const read = deferred<Item[]>();
    reads.push(read);
    return read.promise;
  });
  const watch = vi.fn((options: WatchOptions<Item, number>): EventSubscription => {
    watchOptions = options;
    return { ready: subscribed.promise, unsubscribe };
  });
  const target = { pkFieldName: 'id', watch, list };
  const onRows = vi.fn();
  const onLoadError = vi.fn();
  const onLoading = vi.fn();
  const onError = vi.fn();
  const follow = <R = Item[]>(options: Partial<FollowAnyOptions<R, Item, number>> = {}) =>
    followRecords<R, Item, number>(target, { onRows, onLoadError, onLoading, onError, ...options });
  return {
    target,
    watch,
    list,
    reads,
    unsubscribe,
    onRows,
    onLoadError,
    onLoading,
    onError,
    follow,
    options: () => watchOptions!,
    emit: (change: RecordsChanged<Item, number>) => watchOptions!.onChange(change, { version: 1 } as never),
    resync: () => watchOptions!.onResync(),
    answer: async () => {
      subscribed.resolve();
      await flush();
    },
    refuse: async (error: unknown) => {
      watchOptions!.onError?.(error);
      subscribed.reject(error);
      await flush();
    },
  };
}

/** Collects what `rethrowLater` schedules instead of throwing it. */
function captureRethrows() {
  const thrown: unknown[] = [];
  const spy = vi.spyOn(globalThis, 'queueMicrotask').mockImplementation((task) => {
    try {
      task();
    } catch (error) {
      thrown.push(error);
    }
  });
  return { thrown, restore: () => spy.mockRestore() };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('followRecords: subscription and first read', () => {
  it('subscribes at once with fetch and topic, and reads only after the subscription is answered', async () => {
    const h = harness();
    h.follow({ fetch: true, topic: '/t' });
    expect(h.watch).toHaveBeenCalledTimes(1);
    expect(h.options().fetch).toBe(true);
    expect(h.options().topic).toBe('/t');
    await flush();
    expect(h.list).not.toHaveBeenCalled();
    await h.answer();
    expect(h.list).toHaveBeenCalledTimes(1);
  });

  it('reads also when the subscription is refused, after onError', async () => {
    const h = harness();
    h.follow();
    const error = new Error('refused');
    await h.refuse(error);
    expect(h.onError).toHaveBeenCalledWith(error);
    expect(h.list).toHaveBeenCalledTimes(1);
  });

  it('drops a change replayed before the first read starts', async () => {
    const h = harness();
    const apply = vi.fn((rows: Item[]) => rows);
    h.follow({ apply });
    h.emit(updated(item(1, 'replayed')));
    await h.answer();
    h.reads[0].resolve([item(1)]);
    await flush();
    expect(apply).not.toHaveBeenCalled();
    expect(h.onRows).toHaveBeenCalledWith([item(1)]);
  });

  it('ready resolves once the first read has settled, with rows or a load error', async () => {
    const h = harness();
    const f = h.follow();
    let ready = false;
    void f.ready.then(() => (ready = true));
    await h.answer();
    expect(ready).toBe(false);
    h.reads[0].reject(new Error('down'));
    await flush();
    expect(ready).toBe(true);
    expect(h.onLoadError).toHaveBeenCalledTimes(1);
    expect(f.rows).toBeUndefined();
  });

  it('a reload() before the subscription is answered is the first read', async () => {
    const h = harness();
    const f = h.follow();
    void f.reload();
    expect(h.list).toHaveBeenCalledTimes(1);
    await h.answer();
    expect(h.list).toHaveBeenCalledTimes(1);
    h.reads[0].resolve([item(1)]);
    await f.ready;
    expect(f.rows).toEqual([item(1)]);
  });
});

describe('followRecords: superseded reads', () => {
  it('drops the late answer of an older read, also a failure', async () => {
    const h = harness();
    const f = h.follow();
    await h.answer();
    const second = f.reload();
    h.reads[1].resolve([item(2)]);
    await second;
    h.reads[0].reject(new Error('late'));
    await flush();
    expect(h.onRows.mock.calls).toEqual([[[item(2)]]]);
    expect(h.onLoadError).not.toHaveBeenCalled();
    expect(f.rows).toEqual([item(2)]);

    const third = f.reload();
    const fourth = f.reload();
    h.reads[3].resolve([item(4)]);
    h.reads[2].resolve([item(3)]);
    await Promise.all([third, fourth]);
    expect(h.onRows).toHaveBeenLastCalledWith([item(4)]);
    expect(h.onRows).toHaveBeenCalledTimes(2);
  });

  it('a read that throws synchronously is a failed read', async () => {
    const h = harness();
    const error = new Error('sync');
    h.follow({
      read: () => {
        throw error;
      },
    });
    await h.answer();
    expect(h.onLoadError).toHaveBeenCalledWith(error);
  });
});

describe('followRecords: changes', () => {
  it('holds a change that arrives during a read and applies it after, with one onRows', async () => {
    const h = harness();
    const f = h.follow();
    await h.answer();
    h.emit(updated(item(1, 'changed')));
    h.emit(created(item(3)));
    h.reads[0].resolve([item(1), item(2)]);
    await f.settled();
    expect(h.onRows.mock.calls).toEqual([[[item(1, 'changed'), item(2), item(3)]]]);
  });

  it('discards the changes held before a reload() started', async () => {
    const h = harness();
    let applied = 0;
    const f = h.follow({
      apply: (rows: Item[], change) => {
        applied += 1;
        return rows.concat(change.changes.map((c) => c.record!));
      },
    });
    await h.answer();
    h.emit(created(item(5)));
    void f.reload();
    h.reads[1].resolve([item(1), item(5)]);
    await f.settled();
    expect(applied).toBe(0);
    expect(f.rows).toEqual([item(1), item(5)]);
  });

  it('applies a change at once with no read in flight, in arrival order', async () => {
    const h = harness();
    const order: string[] = [];
    const f = h.follow({
      apply: (rows: Item[], change) => {
        order.push(`${change.op}:${change.changes[0].pk}`);
        return rows;
      },
    });
    await h.answer();
    h.reads[0].resolve([]);
    await f.settled();
    h.emit(created(item(1)));
    expect(order).toEqual(['created:1']);
    void f.reload();
    h.emit(updated(item(2)));
    h.emit(deleted(3));
    h.reads[1].resolve([]);
    await f.settled();
    h.emit(deleted(4));
    expect(order).toEqual(['created:1', 'updated:2', 'deleted:3', 'deleted:4']);
  });

  it("'reload' from apply ends the drain, drops the rest and reads in the background", async () => {
    const h = harness();
    const events: CallEvent[] = [];
    const proxy = new RestProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      validateSchema: false,
      axiosInstance: { get: vi.fn(() => Promise.resolve({ data: [item(9)] })) } as unknown as AxiosInstance,
    });
    let reads = 0;
    const apply = vi.fn((rows: Item[], change: RecordsChanged<Item, number>) =>
      change.op === 'created' ? ('reload' as const) : rows,
    );
    const first = deferred<Item[]>();
    const f = h.follow({
      apply,
      read: () => {
        reads += 1;
        return reads === 1 ? first.promise : proxy.list();
      },
    });
    await h.answer();
    h.emit(created(item(2)));
    h.emit(updated(item(3)));
    const stop = onCallActivity((event) => events.push(event));
    try {
      first.resolve([item(1)]);
      await flush();
      await f.settled();
    } finally {
      stop();
    }
    expect(apply).toHaveBeenCalledTimes(1);
    expect(reads).toBe(2);
    expect(events.find((event) => event.type === 'start')).toMatchObject({ background: true });
    expect(h.onRows.mock.calls).toEqual([[[item(9)]]]);
  });

  it('the default apply reloads for a change without record, and applies records by pkFieldName', async () => {
    const h = harness();
    const f = h.follow();
    await h.answer();
    h.reads[0].resolve([item(1), item(2)]);
    await f.settled();
    h.emit(updated(item(1, 'one')));
    h.emit(deleted(2));
    h.emit(created(item(3)));
    expect(f.rows).toEqual([item(1, 'one'), item(3)]);
    expect(h.list).toHaveBeenCalledTimes(1);
    h.emit({ op: 'updated', changes: [{ pk: 1 }] });
    expect(h.list).toHaveBeenCalledTimes(2);
  });

  it('onResync reads anew in the background; a failed fetch reaches onError', async () => {
    const h = harness();
    const f = h.follow();
    await h.answer();
    h.reads[0].resolve([item(1)]);
    await f.settled();
    h.options().onError?.(new Error('fetch'));
    h.resync();
    expect(h.onError).toHaveBeenCalledTimes(1);
    expect(h.list).toHaveBeenCalledTimes(2);
    h.reads[1].resolve([item(2)]);
    await f.settled();
    expect(f.rows).toEqual([item(2)]);
  });

  it('a resync before the first read does not start a second read', async () => {
    const h = harness();
    h.follow();
    h.resync();
    await h.answer();
    expect(h.list).toHaveBeenCalledTimes(1);
  });
});

describe('followRecords: apply()', () => {
  it("applies the caller's change during a read, and again after the read's answer with its event", async () => {
    const h = harness();
    const f = h.follow();
    await h.answer();
    h.reads[0].resolve([item(1)]);
    await f.settled();
    void f.reload();
    f.apply(updated(item(1, 'own')));
    expect(f.rows).toEqual([item(1, 'own')]);
    expect(h.onRows).toHaveBeenLastCalledWith([item(1, 'own')]);
    h.emit(updated(item(1, 'own')));
    h.reads[1].resolve([item(1)]);
    await f.settled();
    expect(f.rows).toEqual([item(1, 'own')]);
  });

  it('is held during a read and applied again after it, with no event and with a reset read', async () => {
    const h = harness();
    const f = h.follow();
    await h.refuse(new Error('refused'));
    h.reads[0].resolve([item(1, 'old')]);
    await f.settled();
    void f.reload();
    f.apply(updated(item(1, 'mine')));
    h.reads[1].resolve([item(1, 'old')]);
    await f.settled();
    expect(f.rows).toEqual([item(1, 'mine')]);
    expect(h.onRows.mock.calls.map(([rows]) => rows[0].name)).toEqual(['old', 'mine', 'mine']);

    void f.reload({ reset: true });
    f.apply(updated(item(1, 'again')));
    h.reads[2].resolve([item(1, 'mine')]);
    await f.settled();
    expect(f.rows).toEqual([item(1, 'again')]);
  });

  it('is ignored without rows, and a returned reload starts a new read', async () => {
    const h = harness();
    const apply = vi.fn((rows: Item[], change: RecordsChanged<Item, number>) =>
      change.op === 'created' ? ('reload' as const) : rows,
    );
    const f = h.follow({ apply });
    f.apply(updated(item(1)));
    expect(apply).not.toHaveBeenCalled();
    await h.answer();
    h.reads[0].resolve([item(1)]);
    await f.settled();
    f.apply(created(item(2)));
    expect(h.list).toHaveBeenCalledTimes(2);
  });

  it('reload({ reset: true }) clears rows; apply() then only holds the change until the read lands', async () => {
    const h = harness();
    const f = h.follow();
    await h.answer();
    h.reads[0].resolve([item(1)]);
    await f.settled();
    const calls = h.onRows.mock.calls.length;
    void f.reload({ reset: true });
    expect(f.rows).toBeUndefined();
    f.apply(updated(item(1, 'x')));
    expect(f.rows).toBeUndefined();
    expect(h.onRows).toHaveBeenCalledTimes(calls);
    h.reads[1].resolve([item(7)]);
    await f.settled();
    expect(f.rows).toEqual([item(7), item(1, 'x')]);
    expect(h.onRows).toHaveBeenCalledTimes(calls + 1);
  });
});

describe('followRecords: close()', () => {
  it('during a read: unsubscribes, calls nothing afterwards, settles every promise', async () => {
    const h = harness();
    const f = h.follow();
    await h.answer();
    h.reads[0].resolve([item(1)]);
    await f.settled();
    const reloaded = f.reload();
    expect(h.onLoading).toHaveBeenLastCalledWith(true);
    h.onLoading.mockClear();
    h.emit(updated(item(1, 'x')));
    const settled = f.settled();
    f.close();
    await Promise.all([reloaded, settled]);
    h.reads[1].reject(new Error('late'));
    await flush();
    expect(h.unsubscribe).toHaveBeenCalledTimes(1);
    expect(h.onRows).toHaveBeenCalledTimes(1);
    expect(h.onLoadError).not.toHaveBeenCalled();
    expect(h.onLoading.mock.calls).toEqual([]);
    await f.reload();
    await f.settled();
    f.apply(updated(item(1)));
    expect(h.list).toHaveBeenCalledTimes(2);
  });

  it('before the subscription is answered, ready resolves and no read starts', async () => {
    const h = harness();
    const f = h.follow();
    const settled = f.settled();
    f.close();
    await Promise.all([f.ready, settled]);
    await h.answer();
    expect(h.list).not.toHaveBeenCalled();
  });
});

describe('followRecords: failed reads', () => {
  it('keeps the rows and applies the held changes to them', async () => {
    const h = harness();
    const f = h.follow();
    await h.answer();
    h.reads[0].resolve([item(1)]);
    await f.settled();
    void f.reload();
    h.emit(created(item(2)));
    h.reads[1].reject(new Error('down'));
    await f.settled();
    expect(h.onLoadError).toHaveBeenCalledTimes(1);
    expect(f.rows).toEqual([item(1), item(2)]);
  });

  it('after a failed first read, drops the changes until reload()', async () => {
    const h = harness();
    const apply = vi.fn((rows: Item[]) => rows);
    const f = h.follow({ apply });
    await h.answer();
    h.reads[0].reject(new Error('down'));
    await f.settled();
    h.emit(updated(item(1)));
    expect(apply).not.toHaveBeenCalled();
    void f.reload();
    h.reads[1].resolve([item(1)]);
    await f.settled();
    expect(f.rows).toEqual([item(1)]);
  });
});

describe('followRecords: extend()', () => {
  interface Paged {
    pages: Item[][];
  }
  const merge = (rows: Paged, page: Item[]): Paged => ({ pages: [...rows.pages, page] });

  it('merges a page of the current generation, holding changes without clearing them', async () => {
    const h = harness();
    const apply = vi.fn((rows: Paged, change: RecordsChanged<Item, number>) => ({
      pages: rows.pages.map((page) => page.map((row) => change.changes.find((c) => c.pk === row.id)?.record ?? row)),
    }));
    const first = deferred<Paged>();
    const f = h.follow<Paged>({ read: () => first.promise, apply });
    await h.answer();
    first.resolve({ pages: [[item(1)]] });
    await f.settled();
    const page = deferred<Item[]>();
    const extended = f.extend(() => page.promise, merge);
    h.emit(updated(item(2, 'two')));
    expect(apply).not.toHaveBeenCalled();
    page.resolve([item(2)]);
    await extended;
    expect(f.rows).toEqual({ pages: [[item(1)], [item(2, 'two')]] });
    expect(h.onRows).toHaveBeenCalledTimes(2);
  });

  it('drops a page that lands after reload(), reports a failure, and does nothing before the first rows', async () => {
    const h = harness();
    const reads: Deferred<Paged>[] = [];
    const f = h.follow<Paged>({
      read: () => {
        const read = deferred<Paged>();
        reads.push(read);
        return read.promise;
      },
      apply: (rows) => rows,
    });
    const before = vi.fn(() => Promise.resolve([item(0)]));
    await f.extend(before, merge);
    expect(before).not.toHaveBeenCalled();
    await h.answer();
    reads[0].resolve({ pages: [[item(1)]] });
    await f.settled();

    const page = deferred<Item[]>();
    const extended = f.extend(() => page.promise, merge);
    void f.reload();
    page.resolve([item(2)]);
    await extended;
    reads[1].resolve({ pages: [[item(5)]] });
    await f.settled();
    expect(f.rows).toEqual({ pages: [[item(5)]] });

    const error = new Error('page');
    await f.extend(() => Promise.reject(error), merge);
    expect(h.onLoadError).toHaveBeenCalledWith(error);
    expect(f.rows).toEqual({ pages: [[item(5)]] });
  });

  it('does nothing while the read of a new generation is in flight, also when the rows are kept', async () => {
    const h = harness();
    const f = h.follow();
    await h.answer();
    h.reads[0].resolve([item(0), item(1), item(2), item(3)]);
    await f.settled();
    void f.reload();
    const page = vi.fn(() => Promise.resolve([item(4), item(5)]));
    await f.extend(page, (rows, more) => [...rows, ...more]);
    expect(page).not.toHaveBeenCalled();
    h.reads[1].resolve([item(0), item(1)]);
    await f.settled();
    expect(f.rows).toEqual([item(0), item(1)]);
    await f.extend(page, (rows, more) => [...rows, ...more]);
    expect(f.rows).toEqual([item(0), item(1), item(4), item(5)]);
  });
});

describe('followRecords: settled() and onLoading', () => {
  it('settled() waits for a read started while it waited; onLoading reports the latest generation', async () => {
    const h = harness();
    const f = h.follow();
    await h.answer();
    let settled = false;
    void f.settled().then(() => (settled = true));
    void f.reload();
    h.reads[0].resolve([item(1)]);
    await flush();
    expect(settled).toBe(false);
    h.reads[1].resolve([item(2)]);
    await flush();
    expect(settled).toBe(true);
    expect(h.onLoading.mock.calls).toEqual([[true], [false]]);
  });
});

describe('followRecords: callbacks', () => {
  it('a throwing callback is rethrown later and following continues', async () => {
    const rethrows = captureRethrows();
    const h = harness();
    const error = new Error('apply');
    const f = h.follow({
      apply: (rows: Item[], change) => {
        if (change.changes[0].pk === 1) throw error;
        return rows.concat(change.changes.map((c) => c.record!));
      },
    });
    h.onRows.mockImplementationOnce(() => {
      throw new Error('rows');
    });
    await h.answer();
    h.reads[0].resolve([]);
    await f.settled();
    h.emit(created(item(1)));
    h.emit(created(item(2)));
    expect(f.rows).toEqual([item(2)]);
    expect(rethrows.thrown.map((e) => (e as Error).message)).toEqual(['rows', 'apply']);
    rethrows.restore();
  });

  it('a throwing merge keeps the rows, and a throwing apply() leaves them as they are', async () => {
    const rethrows = captureRethrows();
    const h = harness();
    const f = h.follow({
      apply: () => {
        throw new Error('apply');
      },
    });
    await h.answer();
    h.reads[0].resolve([item(1)]);
    await f.settled();
    await f.extend(
      () => Promise.resolve([item(2)]),
      () => {
        throw new Error('merge');
      },
    );
    f.apply(updated(item(1, 'x')));
    expect(f.rows).toEqual([item(1)]);
    expect(rethrows.thrown.map((e) => (e as Error).message)).toEqual(['merge', 'apply']);
    rethrows.restore();
  });

  it('close() or reload() from inside onRows ends the drain', async () => {
    const h = harness();
    const f = h.follow();
    h.onRows.mockImplementationOnce(() => f.reload());
    await h.answer();
    h.reads[0].resolve([item(1)]);
    await flush();
    expect(h.list).toHaveBeenCalledTimes(2);
    expect(h.onLoading).not.toHaveBeenLastCalledWith(false);
    h.onRows.mockImplementationOnce(() => f.close());
    h.reads[1].resolve([item(2)]);
    await flush();
    expect(h.unsubscribe).toHaveBeenCalledTimes(1);
    expect(h.onLoading).not.toHaveBeenLastCalledWith(false);
  });
});

describe('follow() on a REST ViewSet', () => {
  function fakeClient() {
    const subscriptions: { topic: string; handlers: EventHandlers<any> }[] = [];
    const client: EventClient = {
      basePath: '/events',
      subscribe(topic, _event, handlers) {
        subscriptions.push({ topic, handlers });
        return { ready: Promise.resolve(), unsubscribe: vi.fn() };
      },
      close: vi.fn(),
    };
    return { client, subscriptions };
  }

  it('reads with list() by default, on the topic option, and applies records_changed', async () => {
    const { client, subscriptions } = fakeClient();
    const get = vi.fn(() => Promise.resolve({ data: [item(1)] }));
    const proxy = new RestProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      validateSchema: false,
      axiosInstance: { get } as unknown as AxiosInstance,
      events: client,
    });
    const onRows = vi.fn();
    const f = proxy.follow({ topic: '/t', onRows, onLoadError: vi.fn() });
    await f.ready;
    expect(subscriptions[0].topic).toBe('/t');
    expect(get).toHaveBeenCalledTimes(1);
    expect(onRows).toHaveBeenCalledWith([item(1)]);
    subscriptions[0].handlers.onEvent(deleted(1), { version: 1 } as never);
    await flush();
    expect(f.rows).toEqual([]);
  });
});
