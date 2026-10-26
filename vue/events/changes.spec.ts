/**
 * Unit tests for changes.ts: `applyChanges`, and `watch()` on both proxies, on a factory-built
 * ViewSet and on a `route_rest` / `route_muxws` proxy.
 */

import type { AxiosInstance } from 'axios';
import { afterEach, describe, expect, expectTypeOf, it, vi } from 'vitest';

import { type CallEvent, onCallActivity } from '../call';
import { ViewSetRequestError } from '../errors';
import { type LookupItem, ReadOnlyViewSetMixin, RetrieveMixin } from '../mixins';
import { type MuxwsPeerLike, MuxwsProxyImpl, route_muxws } from '../muxws-proxy';
import { RestProxyImpl, route_rest } from '../rest-proxy';
import { restViewSet } from '../viewset';

import { applyChanges, RECORDS_CHANGED, type RecordsChanged } from './changes';
import {
  createEventClient,
  type EventClient,
  type EventHandlers,
  type EventMeta,
  type EventSubscription,
} from './client';

interface Item {
  id: number;
  name: string;
}

const rows: Item[] = [
  { id: 1, name: 'one' },
  { id: 2, name: 'two' },
  { id: 3, name: 'three' },
];

describe('applyChanges', () => {
  it('deleted removes the rows with the changed pks', () => {
    const out = applyChanges(rows, { op: 'deleted', changes: [{ pk: 1 }, { pk: 3 }] }, 'id');
    expect(out).toEqual([{ id: 2, name: 'two' }]);
  });

  it('updated replaces the row with the same pk, in place', () => {
    const out = applyChanges(rows, { op: 'updated', changes: [{ pk: 2, record: { id: 2, name: 'TWO' } }] }, 'id');
    expect(out).toEqual([
      { id: 1, name: 'one' },
      { id: 2, name: 'TWO' },
      { id: 3, name: 'three' },
    ]);
  });

  it('created and updated append a record no row has, in the order of the changes', () => {
    const out = applyChanges(
      rows,
      {
        op: 'created',
        changes: [
          { pk: 5, record: { id: 5, name: 'five' } },
          { pk: 4, record: { id: 4, name: 'four' } },
        ],
      },
      'id',
    );
    expect(out.map((row) => row.id)).toEqual([1, 2, 3, 5, 4]);
    expect(
      applyChanges(rows, { op: 'updated', changes: [{ pk: 9, record: { id: 9, name: 'n' } }] }, 'id'),
    ).toHaveLength(4);
  });

  it('skips a change without record', () => {
    const out = applyChanges(rows, { op: 'updated', changes: [{ pk: 1 }, { pk: 2, record: null }] }, 'id');
    expect(out).toEqual(rows);
  });

  it('returns a new array and leaves rows untouched', () => {
    const before = rows.map((row) => ({ ...row }));
    const out = applyChanges(rows, { op: 'deleted', changes: [] }, 'id');
    expect(out).not.toBe(rows);
    expect(out).toEqual(rows);
    applyChanges(rows, { op: 'updated', changes: [{ pk: 1, record: { id: 1, name: 'x' } }] }, 'id');
    expect(rows).toEqual(before);
  });

  it('compares pks by their string form', () => {
    const changed: RecordsChanged<Item, number | string> = { op: 'deleted', changes: [{ pk: '2' }] };
    expect(applyChanges(rows, changed, 'id').map((row) => row.id)).toEqual([1, 3]);
  });

  it('is idempotent per record: applying one change twice ends in the same state', () => {
    const change: RecordsChanged<Item, number> = {
      op: 'created',
      changes: [{ pk: 4, record: { id: 4, name: 'four' } }],
    };
    const once = applyChanges(rows, change, 'id');
    expect(applyChanges(once, change, 'id')).toEqual(once);
  });
});

/** The `EventMeta` of an event of `/items`. */
function meta(rev = 11, version = 1): EventMeta {
  return { topic: '/items', rev, prev: rev - 1, version };
}

/** An EventClient that records the subscription `watch()` makes and hands its handlers to the test. */
function fakeClient() {
  const subscriptions: { topic: string; event: string; handlers: EventHandlers<any>; sub: EventSubscription }[] = [];
  const client: EventClient = {
    basePath: '/events',
    subscribe(topic, event, handlers) {
      const sub: EventSubscription = { ready: Promise.resolve(), unsubscribe: vi.fn(), onError: handlers.onError };
      subscriptions.push({ topic, event: event.name, handlers, sub });
      return sub;
    },
    close: vi.fn(),
  };
  return { client, subscriptions };
}

function mockAxios(get: (url: string) => Promise<{ data: unknown }>) {
  return {
    get: vi.fn(get),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  } as unknown as AxiosInstance & {
    get: ReturnType<typeof vi.fn>;
  };
}

function notFound(): Promise<never> {
  return Promise.reject(Object.assign(new Error('404'), { response: { status: 404, data: { detail: 'Not found' } } }));
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('watch() on a REST ViewSet', () => {
  it('throws without an event client', () => {
    const proxy = new RestProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      validateSchema: false,
    });
    expect(() => proxy.watch({ onChange() {}, onResync() {} })).toThrow(/needs an event client/);
  });

  it('subscribes basePath to records_changed on RestProxyOptions.events, and its subscription reaches it', async () => {
    const { client, subscriptions } = fakeClient();
    const proxy = new RestProxyImpl<number, Item, 'id'>({
      basePath: '/items/',
      pkFieldName: 'id',
      validateSchema: false,
      events: client,
    });
    const sub = proxy.watch({ onChange: vi.fn(), onResync: vi.fn() });
    expect(subscriptions).toHaveLength(1);
    expect(subscriptions[0]).toMatchObject({ topic: '/items', event: RECORDS_CHANGED.name });
    await expect(sub.ready).resolves.toBeUndefined();
    sub.unsubscribe();
    expect(subscriptions[0].sub.unsubscribe).toHaveBeenCalledTimes(1);
  });

  it('calls no option after unsubscribe(), also for steps queued before it', async () => {
    const { client, subscriptions } = fakeClient();
    let release!: () => void;
    const http = mockAxios(
      () =>
        new Promise((resolve) => {
          release = () => resolve({ data: { id: 4, name: 'late' } });
        }),
    );
    const proxy = new RestProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      validateSchema: false,
      axiosInstance: http,
      events: client,
    });
    const onChange = vi.fn();
    const onResync = vi.fn();
    const sub = proxy.watch({ fetch: true, onChange, onResync });
    const { handlers } = subscriptions[0];
    handlers.onEvent({ op: 'updated', changes: [{ pk: 4 }] }, meta());
    handlers.onResync();
    handlers.onEvent({ op: 'deleted', changes: [{ pk: 1 }] }, meta());
    await flush();
    sub.unsubscribe();
    release();
    await flush();
    expect(onChange).not.toHaveBeenCalled();
    expect(onResync).not.toHaveBeenCalled();
  });

  it('calls onChange once per event and onResync on a resync, in arrival order', async () => {
    const { client, subscriptions } = fakeClient();
    const proxy = new RestProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      validateSchema: false,
      events: client,
    });
    const seen: unknown[] = [];
    proxy.watch({ onChange: (change) => seen.push(change), onResync: () => seen.push('resync') });
    const { handlers } = subscriptions[0];
    handlers.onEvent({ op: 'deleted', changes: [{ pk: 1 }] }, meta());
    handlers.onResync();
    handlers.onEvent({ op: 'updated', changes: [{ pk: 2 }] }, meta());
    await flush();
    expect(seen).toEqual([{ op: 'deleted', changes: [{ pk: 1 }] }, 'resync', { op: 'updated', changes: [{ pk: 2 }] }]);
  });

  it("passes the event's EventMeta to onChange, also after fetch", async () => {
    const { client, subscriptions } = fakeClient();
    const http = mockAxios(() => Promise.resolve({ data: { id: 4, name: 'fetched' } }));
    const proxy = new RestProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      validateSchema: false,
      axiosInstance: http,
      events: client,
    });
    const seen: [string, number, number][] = [];
    proxy.watch({ onChange: (change, { rev, version }) => seen.push([change.op, rev, version]), onResync() {} });
    proxy.watch({
      fetch: true,
      onChange: (change, { rev, version }) => seen.push([`fetched ${change.op}`, rev, version]),
      onResync() {},
    });
    subscriptions[0].handlers.onEvent({ op: 'deleted', changes: [{ pk: 1 }] }, meta(11, 1));
    subscriptions[1].handlers.onEvent({ op: 'updated', changes: [{ pk: 4 }] }, meta(12, 1));
    await flush();
    expect(seen).toEqual([
      ['deleted', 11, 1],
      ['fetched updated', 12, 1],
    ]);
  });

  it.each([false, true])(
    'calls onResync, not onChange, for an event whose version is not 1 (fetch: %s)',
    async (fetch) => {
      const { client, subscriptions } = fakeClient();
      const retrieve = vi.fn(() => Promise.resolve({ data: { id: 4, name: 'fetched' } }));
      const proxy = new RestProxyImpl<number, Item, 'id'>({
        basePath: '/items',
        pkFieldName: 'id',
        validateSchema: false,
        axiosInstance: mockAxios(retrieve),
        events: client,
      });
      const seen: unknown[] = [];
      proxy.watch({ fetch, onChange: (change) => seen.push(change), onResync: () => seen.push('resync') });
      subscriptions[0].handlers.onEvent({ op: 'updated', items: [{ id: 4 }] }, meta(11, 2));
      subscriptions[0].handlers.onEvent(
        { op: 'updated', changes: [{ pk: 4, record: { id: 4, name: 'x' } }], actor: 3 },
        meta(12, 2),
      );
      subscriptions[0].handlers.onEvent({ op: 'deleted', changes: [{ pk: 4 }] }, meta(13, 1));
      await flush();
      expect(seen).toEqual(['resync', 'resync', { op: 'deleted', changes: [{ pk: 4 }] }]);
      expect(retrieve).not.toHaveBeenCalled();
    },
  );

  it('passes onError to the subscription', () => {
    const { client, subscriptions } = fakeClient();
    const proxy = new RestProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      validateSchema: false,
      events: client,
    });
    const onError = vi.fn();
    proxy.watch({ onChange() {}, onResync() {}, onError });
    const error = new ViewSetRequestError(403, {});
    subscriptions[0].handlers.onError?.(error);
    expect(onError).toHaveBeenCalledWith(error);
  });

  it('subscribes WatchOptions.topic, else ProxyBaseOptions.topic, else basePath', () => {
    const { client, subscriptions } = fakeClient();
    const options = {
      basePath: '/app/items',
      pkFieldName: 'id',
      validateSchema: false,
      events: client,
    };
    const plain = new RestProxyImpl<number, Item, 'id'>(options);
    const mounted = new RestProxyImpl<number, Item, 'id'>({
      ...options,
      topic: '/items',
    });
    plain.watch({ onChange() {}, onResync() {} });
    mounted.watch({ onChange() {}, onResync() {} });
    mounted.watch({ onChange() {}, onResync() {}, topic: '/orgs/7/items' });
    expect(subscriptions.map(({ topic }) => topic)).toEqual(['/app/items', '/items', '/orgs/7/items']);
  });

  describe('fetch: true', () => {
    function build(get: (url: string) => Promise<{ data: unknown }>) {
      const { client, subscriptions } = fakeClient();
      const http = mockAxios(get);
      const proxy = new RestProxyImpl<number, Item, 'id'>({
        basePath: '/items',
        pkFieldName: 'id',
        validateSchema: false,
        axiosInstance: http,
        events: client,
      });
      const onChange = vi.fn();
      const onResync = vi.fn();
      const onError = vi.fn();
      proxy.watch({ fetch: true, onChange, onResync, onError });
      const { onEvent } = subscriptions[0].handlers;
      return { http, onChange, onResync, onError, emit: (payload: unknown) => onEvent(payload, meta()) };
    }

    it('issues the retrieves as background calls, also through a retrieve override', async () => {
      const events: CallEvent[] = [];
      const stop = onCallActivity((event) => events.push(event));
      try {
        const w = build((url) =>
          Promise.resolve({
            data: { id: Number(url.split('/').pop()), name: 'fetched' },
          }),
        );
        w.emit({ op: 'created', changes: [{ pk: 4 }] });
        await flush();

        const { client, subscriptions } = fakeClient();
        class Overriding extends RestProxyImpl<number, Item, 'id'> {
          override retrieve(pk: number) {
            return super.retrieve(pk).then((item) => ({ ...item, name: 'overridden' }));
          }
        }
        const http = mockAxios((url) =>
          Promise.resolve({
            data: { id: Number(url.split('/').pop()), name: 'x' },
          }),
        );
        const onChange = vi.fn();
        new Overriding({
          basePath: '/items',
          pkFieldName: 'id',
          validateSchema: false,
          axiosInstance: http,
          events: client,
        }).watch({ fetch: true, onChange, onResync() {} });
        subscriptions[0].handlers.onEvent({ op: 'updated', changes: [{ pk: 5 }] }, meta());
        await flush();
        expect(onChange.mock.calls[0][0].changes[0].record).toEqual({
          id: 5,
          name: 'overridden',
        });
      } finally {
        stop();
      }
      const starts = events.filter((event) => event.type === 'start');
      expect(starts).toHaveLength(2);
      for (const start of starts) expect(start).toMatchObject({ background: true });
    });

    it('completes a created or updated change without record with retrieve(pk)', async () => {
      const w = build((url) => Promise.resolve({ data: { id: Number(url.split('/').pop()), name: 'fetched' } }));
      w.emit({ op: 'created', changes: [{ pk: 4 }, { pk: 5, record: { id: 5, name: 'sent' } }] });
      await flush();
      expect(w.http.get).toHaveBeenCalledTimes(1);
      expect(w.http.get.mock.calls[0][0]).toBe('/items/4');
      expect(w.onChange).toHaveBeenCalledWith(
        {
          op: 'created',
          changes: [
            { pk: 4, record: { id: 4, name: 'fetched' } },
            { pk: 5, record: { id: 5, name: 'sent' } },
          ],
        },
        meta(),
      );
    });

    it('passes the changes whose retrieve answers 404 as deleted, after the others', async () => {
      const w = build((url) => (url === '/items/6' ? Promise.resolve({ data: { id: 6, name: 'six' } }) : notFound()));
      w.emit({ op: 'updated', changes: [{ pk: 4 }, { pk: 6 }, { pk: 7 }] });
      await flush();
      expect(w.onChange.mock.calls).toEqual([
        [{ op: 'updated', changes: [{ pk: 6, record: { id: 6, name: 'six' } }] }, meta()],
        [{ op: 'deleted', changes: [{ pk: 4 }, { pk: 7 }] }, meta()],
      ]);
      expect(w.onError).not.toHaveBeenCalled();
    });

    it('calls onChange once, with deleted, when every retrieve answers 404', async () => {
      const w = build(() => notFound());
      w.emit({ op: 'updated', changes: [{ pk: 4 }] });
      await flush();
      expect(w.onChange.mock.calls).toEqual([[{ op: 'deleted', changes: [{ pk: 4 }] }, meta()]]);
      expect(w.onResync).not.toHaveBeenCalled();
      expect(w.onError).not.toHaveBeenCalled();
    });

    it('removes a displayed record whose retrieve answers 404 with applyChanges', async () => {
      const w = build(() => notFound());
      let rows = [
        { id: 4, name: 'four' },
        { id: 5, name: 'five' },
      ];
      w.onChange.mockImplementation((change: RecordsChanged<{ id: number; name: string }, number>) => {
        rows = applyChanges(rows, change, 'id');
      });
      w.emit({ op: 'updated', changes: [{ pk: 4 }] });
      await flush();
      expect(rows).toEqual([{ id: 5, name: 'five' }]);
    });

    it('retrieves nothing for a deleted change', async () => {
      const w = build(() => Promise.resolve({ data: {} }));
      w.emit({ op: 'deleted', changes: [{ pk: 4 }] });
      await flush();
      expect(w.http.get).not.toHaveBeenCalled();
      expect(w.onChange).toHaveBeenCalledWith({ op: 'deleted', changes: [{ pk: 4 }] }, meta());
    });

    it('reports a retrieve that fails with anything but 404 to onError, and resyncs', async () => {
      const failure = Object.assign(new Error('500'), { response: { status: 500 } });
      const w = build(() => Promise.reject(failure));
      w.emit({ op: 'updated', changes: [{ pk: 4 }] });
      await flush();
      expect(w.onChange).not.toHaveBeenCalled();
      expect(w.onError).toHaveBeenCalledWith(failure);
      expect(w.onResync).toHaveBeenCalledTimes(1);
    });

    it('keeps the event order while a retrieve is pending', async () => {
      let release!: () => void;
      const w = build(
        () =>
          new Promise((resolve) => {
            release = () => resolve({ data: { id: 4, name: 'late' } });
          }),
      );
      w.emit({ op: 'updated', changes: [{ pk: 4 }] });
      w.emit({ op: 'deleted', changes: [{ pk: 1 }] });
      await flush();
      expect(w.onChange).not.toHaveBeenCalled();
      release();
      await flush();
      expect(w.onChange.mock.calls.map(([change]) => change.op)).toEqual(['updated', 'deleted']);
    });
  });

  describe('fetch: a function', () => {
    interface ItemRow {
      id: number;
      label: string;
    }

    function build(fetch: (pks: number[]) => Promise<readonly ItemRow[]>) {
      const { client, subscriptions } = fakeClient();
      const http = mockAxios(() => Promise.reject(new Error('fetch with a function retrieves nothing')));
      const proxy = new RestProxyImpl<number, ItemRow, 'id'>({
        basePath: '/items',
        pkFieldName: 'id',
        validateSchema: false,
        axiosInstance: http,
        events: client,
      });
      const onChange = vi.fn();
      const onResync = vi.fn();
      const onError = vi.fn();
      const fetchRows = vi.fn(fetch);
      proxy.watch({ fetch: fetchRows, onChange, onResync, onError });
      const { onEvent } = subscriptions[0].handlers;
      return { http, fetchRows, onChange, onResync, onError, emit: (payload: unknown) => onEvent(payload, meta()) };
    }

    it('completes the changes without record with one call, matching the rows by pk', async () => {
      const w = build((pks) => Promise.resolve(pks.map((pk) => ({ id: Number(pk), label: `row ${pk}` })).reverse()));
      w.emit({ op: 'updated', changes: [{ pk: 4 }, { pk: 5, record: { id: 5, label: 'sent' } }, { pk: '6' }] });
      await flush();
      expect(w.fetchRows.mock.calls).toEqual([[[4, '6']]]);
      expect(w.http.get).not.toHaveBeenCalled();
      expect(w.onChange.mock.calls).toEqual([
        [
          {
            op: 'updated',
            changes: [
              { pk: 4, record: { id: 4, label: 'row 4' } },
              { pk: 5, record: { id: 5, label: 'sent' } },
              { pk: '6', record: { id: 6, label: 'row 6' } },
            ],
          },
          meta(),
        ],
      ]);
    });

    it('passes the pks it returns no row for as deleted, after the others', async () => {
      const w = build(() => Promise.resolve([{ id: 6, label: 'six' }]));
      w.emit({ op: 'created', changes: [{ pk: 4 }, { pk: 6 }, { pk: 7 }] });
      await flush();
      expect(w.onChange.mock.calls).toEqual([
        [{ op: 'created', changes: [{ pk: 6, record: { id: 6, label: 'six' } }] }, meta()],
        [{ op: 'deleted', changes: [{ pk: 4 }, { pk: 7 }] }, meta()],
      ]);
    });

    it('calls onChange once, with deleted, when it returns no row', async () => {
      const w = build(() => Promise.resolve([]));
      w.emit({ op: 'updated', changes: [{ pk: 4 }] });
      await flush();
      expect(w.onChange.mock.calls).toEqual([[{ op: 'deleted', changes: [{ pk: 4 }] }, meta()]]);
    });

    it('is not called for a deleted change or for one whose every change carries its record', async () => {
      const w = build(() => Promise.resolve([]));
      w.emit({ op: 'deleted', changes: [{ pk: 4 }] });
      w.emit({ op: 'updated', changes: [{ pk: 5, record: { id: 5, label: 'sent' } }] });
      await flush();
      expect(w.fetchRows).not.toHaveBeenCalled();
      expect(w.onChange.mock.calls.map(([change]) => change.op)).toEqual(['deleted', 'updated']);
    });

    it('reports a rejection to onError, and resyncs', async () => {
      const failure = Object.assign(new Error('404'), { response: { status: 404 } });
      const w = build(() => Promise.reject(failure));
      w.emit({ op: 'updated', changes: [{ pk: 4 }] });
      await flush();
      expect(w.onChange).not.toHaveBeenCalled();
      expect(w.onError).toHaveBeenCalledWith(failure);
      expect(w.onResync).toHaveBeenCalledTimes(1);
    });

    it('issues the calls of the function as background calls', async () => {
      const events: CallEvent[] = [];
      const stop = onCallActivity((event) => events.push(event));
      try {
        const { client, subscriptions } = fakeClient();
        const http = mockAxios(() => Promise.resolve({ data: [{ id: 4, label: 'listed' }] }));
        class RowApi extends restViewSet<Item, LookupItem, { compact: ItemRow }, ItemRow>()('id', [
          ReadOnlyViewSetMixin,
        ]) {}
        const api = new RowApi({ basePath: '/items', validateSchema: false, axiosInstance: http, events: client });
        const onChange = vi.fn();
        api.watch({ fetch: (pks) => api.list({ ids: pks }), onChange, onResync() {} });
        subscriptions[0].handlers.onEvent({ op: 'updated', changes: [{ pk: 4 }] }, meta());
        await flush();
        expect(http.get).toHaveBeenCalledWith('/items', expect.objectContaining({ params: { ids: [4] } }));
        expect(onChange.mock.calls[0][0].changes).toEqual([{ pk: 4, record: { id: 4, label: 'listed' } }]);
      } finally {
        stop();
      }
      const starts = events.filter((event) => event.type === 'start');
      expect(starts).toHaveLength(1);
      expect(starts[0]).toMatchObject({ background: true });
    });
  });
});

/** A peer whose every stream is answered with `{ rev: 1 }`, recording what was opened on it. */
function answeringPeer() {
  const opened: { payload: unknown; headers: Record<string, unknown> }[] = [];
  const peer: MuxwsPeerLike = {
    isOpen: true,
    open(payload, options) {
      opened.push({ payload, headers: (options?.headers ?? {}) as Record<string, unknown> });
      return {
        result: () => Promise.resolve({ rev: 1 }),
        replyHeadersArrived: Promise.resolve(),
        replyHeaders: { ':status': 200 },
      };
    },
  };
  return { peer, opened };
}

describe('watch() on a muxws ViewSet', () => {
  const clients: EventClient[] = [];
  afterEach(() => {
    for (const client of clients.splice(0)) client.close();
  });

  it('subscribes on the client registered for its peer', async () => {
    const { peer, opened } = answeringPeer();
    clients.push(createEventClient({ basePath: '/events', peer }));
    const proxy = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer,
      validateSchema: false,
    });
    const sub = proxy.watch({ onChange() {}, onResync() {} });
    await sub.ready;
    expect(opened).toEqual([
      { payload: { topic: '/items' }, headers: { ':method': 'POST', ':path': '/events/subscribe' } },
    ]);
  });

  it('throws for a peer without a client', () => {
    const { peer } = answeringPeer();
    const proxy = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer,
      validateSchema: false,
    });
    expect(() => proxy.watch({ onChange() {}, onResync() {} })).toThrow(/no event client/);
  });

  it('uses `events` from its options instead of the peer client', () => {
    const { peer } = answeringPeer();
    const { client, subscriptions } = fakeClient();
    const proxy = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer,
      validateSchema: false,
      events: client,
    });
    proxy.watch({ onChange() {}, onResync() {} });
    expect(subscriptions).toHaveLength(1);
  });

  it('subscribes once a peer source that returns a promise has resolved', async () => {
    const { peer, opened } = answeringPeer();
    clients.push(createEventClient({ basePath: '/events', peer }));
    const proxy = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer: () => Promise.resolve(peer),
      validateSchema: false,
    });
    const sub = proxy.watch({ onChange() {}, onResync() {} });
    expect(opened).toEqual([]);
    await sub.ready;
    expect(opened).toHaveLength(1);
  });

  it('a deferred watch unsubscribed before its peer resolves subscribes nothing', async () => {
    const { peer, opened } = answeringPeer();
    clients.push(createEventClient({ basePath: '/events', peer }));
    const proxy = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer: () => Promise.resolve(peer),
      validateSchema: false,
    });
    const sub = proxy.watch({ onChange() {}, onResync() {} });
    sub.unsubscribe();
    await sub.ready;
    expect(opened).toEqual([]);
  });

  it('a deferred watch rejects ready for a peer without a client and passes the error to onError', async () => {
    const { peer } = answeringPeer();
    const proxy = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer: () => Promise.resolve(peer),
      validateSchema: false,
    });
    const onError = vi.fn();
    const sub = proxy.watch({ onChange() {}, onResync() {}, onError });
    await expect(sub.ready).rejects.toThrow(/no event client/);
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ message: expect.stringMatching(/no event client/) }),
    );
  });

  it('a deferred watch without onError rethrows the error of a peer without a client in a microtask', async () => {
    const { peer } = answeringPeer();
    const proxy = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer: () => Promise.resolve(peer),
      validateSchema: false,
    });
    const scheduled: (() => void)[] = [];
    const spy = vi.spyOn(globalThis, 'queueMicrotask').mockImplementation((task) => scheduled.push(task));
    try {
      const sub = proxy.watch({ onChange() {}, onResync() {} });
      await expect(sub.ready).rejects.toThrow(/no event client/);
      expect(scheduled).toHaveLength(1);
      expect(scheduled[0]).toThrow(/no event client/);
    } finally {
      spy.mockRestore();
    }
  });

  it('a deferred watch passes the error of a closed client to onError', async () => {
    const { peer } = answeringPeer();
    const closed = createEventClient({ basePath: '/events', peer });
    closed.close();
    class Deferred extends MuxwsProxyImpl<number, Item, 'id'> {
      protected override eventClient(): Promise<EventClient> {
        return Promise.resolve(closed);
      }
    }
    const deferred = new Deferred({ basePath: '/items', pkFieldName: 'id', peer, validateSchema: false });
    const onError = vi.fn();
    const sub = deferred.watch({ onChange() {}, onResync() {}, onError });
    await expect(sub.ready).rejects.toThrow(/closed/);
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it('a deferred watch passes a refusal to the subscription onError', async () => {
    const { peer } = answeringPeer();
    const { client, subscriptions } = fakeClient();
    // `eventClient()` is protected; a subclass reaches it to hand back a client that resolves later.
    class Deferred extends MuxwsProxyImpl<number, Item, 'id'> {
      protected override eventClient(): Promise<EventClient> {
        return Promise.resolve(client);
      }
    }
    const deferred = new Deferred({ basePath: '/items', pkFieldName: 'id', peer, validateSchema: false });
    const first = vi.fn();
    const sub = deferred.watch({ onChange() {}, onResync() {}, onError: first });
    await sub.ready;
    subscriptions[0].sub.onError?.('refused');
    expect(first).toHaveBeenCalledWith('refused');
    const second = vi.fn();
    sub.onError = second;
    subscriptions[0].sub.onError?.('again');
    expect(second).toHaveBeenCalledWith('again');
  });
});

describe('watch() on the public surface', () => {
  it('exists on a factory-built ViewSet, typed with its record and pk', () => {
    const { client, subscriptions } = fakeClient();
    class ItemApi extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin]) {}
    const api = new ItemApi({ basePath: '/items', validateSchema: false, events: client });
    const names: string[] = [];
    api.watch({
      onChange(change) {
        const pk: number = change.changes[0].pk;
        names.push(`${pk}:${change.changes[0].record?.name}`);
      },
      onResync() {},
    });
    subscriptions[0].handlers.onEvent({ op: 'created', changes: [{ pk: 1, record: { id: 1, name: 'one' } }] }, meta());
    return flush().then(() => expect(names).toEqual(['1:one']));
  });

  it('exists on route_rest and route_muxws proxies', () => {
    const { client, subscriptions } = fakeClient();
    abstract class ItemViewSet extends RetrieveMixin<number, Item> {}
    const rest = route_rest<RetrieveMixin<number, Item>>(ItemViewSet, {
      basePath: '/items',
      pkFieldName: 'id',
      validateSchema: false,
      events: client,
    });
    rest.watch({
      onChange(change) {
        const name: string | undefined = change.changes[0].record?.name;
        void name;
      },
      onResync() {},
    });
    const { peer } = answeringPeer();
    const muxws = route_muxws<RetrieveMixin<number, Item>>(ItemViewSet, {
      basePath: '/other',
      pkFieldName: 'id',
      peer,
      validateSchema: false,
      events: client,
    });
    muxws.watch({ onChange() {}, onResync() {} });
    expect(subscriptions.map((s) => s.topic)).toEqual(['/items', '/other']);
  });

  it('a positional route_rest proxy, which takes no events, has no watch() in its type', () => {
    abstract class ItemViewSet extends RetrieveMixin<number, Item> {}
    // The schema check, which the positional form cannot switch off, stays unanswered.
    const http = mockAxios(() => new Promise(() => {}));
    const rest = route_rest<RetrieveMixin<number, Item>>(ItemViewSet, '/items', 'id', http);
    // @ts-expect-error -- the positional form carries no watch()
    expect(() => rest.watch({ onChange() {}, onResync() {} })).toThrow(/needs an event client/);
  });

  it('types the changes with the row type of a list call without listType', () => {
    interface ItemRow {
      id: number;
      label: string;
    }
    const { client } = fakeClient();

    class TypedApi extends restViewSet<Item, LookupItem, { compact: ItemRow }>()('id', [ReadOnlyViewSetMixin]) {}
    const typed = new TypedApi({ basePath: '/items', validateSchema: false, events: client });
    typed.watch({
      fetch: true,
      onChange(change) {
        const name: string | undefined = change.changes[0].record?.name;
        void name;
      },
      onResync() {},
    });
    typed.watch({ fetch: (pks) => typed.list({ ids: pks }), onChange() {}, onResync() {} });
    typed.watch({
      fetch: (pks) => typed.list({ ids: pks }, { listType: 'compact' }),
      onChange(change) {
        const label: string | undefined = change.changes[0].record?.label;
        void label;
      },
      onResync() {},
    });
    // @ts-expect-error -- onChange receives the compact rows the fetch function returns
    typed.watch({
      fetch: (pks) => typed.list({ ids: pks }, { listType: 'compact' }),
      onChange(change: RecordsChanged<Item, number>) {
        void change;
      },
      onResync() {},
    });

    class RowApi extends restViewSet<Item, LookupItem, { compact: ItemRow }, ItemRow>()('id', [ReadOnlyViewSetMixin]) {}
    const api = new RowApi({ basePath: '/items', validateSchema: false, events: client });
    api.watch({
      fetch: (pks) => api.list({ ids: pks }),
      onChange(change) {
        const label: string | undefined = change.changes[0].record?.label;
        void label;
      },
      onResync() {},
    });
    // @ts-expect-error -- fetch: true retrieves records, which are not list rows
    api.watch({ fetch: true, onChange() {}, onResync() {} });
    api.watch({ fetch: false, onChange() {}, onResync() {} });

    type RowMixin = ReadOnlyViewSetMixin<number, Item, { compact: ItemRow }, ItemRow>;
    abstract class RowViewSet extends ReadOnlyViewSetMixin<number, Item, { compact: ItemRow }, ItemRow> {}
    const rest = route_rest<RowMixin>(RowViewSet, {
      basePath: '/items',
      pkFieldName: 'id',
      validateSchema: false,
      events: client,
    });
    expectTypeOf(rest.list()).resolves.toEqualTypeOf<ItemRow[]>();
    expectTypeOf(rest.list({}, { listType: 'compact' })).resolves.toEqualTypeOf<ItemRow[]>();
    expectTypeOf(rest.list().key).toEqualTypeOf<string>();
    expectTypeOf(rest.retrieve(1)).resolves.toEqualTypeOf<Item>();
    // @ts-expect-error -- 'full' is not one of the list types
    void rest.list({}, { listType: 'full' });
    rest.watch({
      fetch: (pks) => rest.list({ ids: pks }),
      onChange(change) {
        // @ts-expect-error -- a list row has no name
        void change.changes[0].record?.name;
      },
      onResync() {},
    });
    // @ts-expect-error -- fetch: true retrieves records, which are not list rows
    rest.watch({ fetch: true, onChange() {}, onResync() {} });

    const plain = route_rest<ReadOnlyViewSetMixin<number, Item>>(RowViewSet, {
      basePath: '/items',
      pkFieldName: 'id',
      validateSchema: false,
      events: client,
    });
    expectTypeOf(plain.list()).resolves.toEqualTypeOf<Item[]>();
    plain.watch({ fetch: true, onChange() {}, onResync() {} });
    expect(true).toBe(true);
  });

  it('rejects an onChange whose parameter type is wider than the record type', () => {
    const { client } = fakeClient();
    const proxy = new RestProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      validateSchema: false,
      events: client,
    });
    proxy.watch({
      // @ts-expect-error -- the record type has no field `extra`
      onChange(change: RecordsChanged<Item & { extra: boolean }, number>) {
        void change;
      },
      onResync() {},
    });
  });

  it('requires onChange and onResync', () => {
    const { client } = fakeClient();
    const proxy = new RestProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      validateSchema: false,
      events: client,
    });
    // @ts-expect-error -- onResync is required
    proxy.watch({ onChange() {} });
    // @ts-expect-error -- onChange is required
    proxy.watch({ onResync() {} });
  });
});
