/**
 * Unit tests for muxws-proxy.ts
 *
 * The peer is faked rather than connected: what matters here is the envelope the proxy puts on
 * the open frame and what it makes of the reply, not muxws' own behaviour, which has its own
 * conformance suite.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { type CallEvent, onCallActivity } from './call';
import { createEventClient } from './events/client';
import { BulkViewSetMixin, LookupMixin } from './mixins';
import { type MuxwsPeerLike, MuxwsProxyImpl, route_muxws } from './muxws-proxy';
import { ListShapeMismatchError, ViewSetRequestError } from './proxy-base';
import { muxwsViewSet } from './viewset';

interface Item {
  id: number;
  name: string;
}

abstract class ItemViewSet extends BulkViewSetMixin<number, Item, 'id'> {}

const MOCK_ITEM: Item = { id: 1, name: 'Test item' };
const MOCK_ITEMS: Item[] = [MOCK_ITEM, { id: 2, name: 'Second item' }];

interface OpenedStream {
  payload: unknown;
  headers: Record<string, unknown>;
  end?: boolean;
}

/**
 * A peer that records what was opened on it and answers with whatever the test queued.
 */
function makeMockPeer() {
  const opened: OpenedStream[] = [];
  let nextBody: unknown = null;
  let nextReplyHeaders: Record<string, unknown> | null = { ':status': 200 };
  let nextError: Error | null = null;

  const peer: MuxwsPeerLike = {
    open(payload, options) {
      opened.push({ payload, headers: (options?.headers ?? {}) as Record<string, unknown>, end: options?.end });
      const body = nextBody;
      const replyHeaders = nextReplyHeaders;
      const error = nextError;
      return {
        result: () => (error ? Promise.reject(error) : Promise.resolve(body)),
        replyHeadersArrived: Promise.resolve(),
        replyHeaders,
      };
    },
  };

  return {
    peer,
    opened,
    reply(body: unknown, replyHeaders: Record<string, unknown> | null = { ':status': 200 }) {
      nextBody = body;
      nextReplyHeaders = replyHeaders;
      nextError = null;
    },
    fail(error: Error) {
      nextError = error;
    },
    get last() {
      return opened[opened.length - 1];
    },
  };
}

describe('MuxwsProxyImpl — envelope', () => {
  let mock: ReturnType<typeof makeMockPeer>;
  let proxy: MuxwsProxyImpl<number, Item, 'id'>;

  beforeEach(() => {
    mock = makeMockPeer();
    proxy = new MuxwsProxyImpl({
      basePath: '/items',
      pkFieldName: 'id',
      peer: mock.peer,
      validateSchema: false,
    });
  });

  it('list() — GET /items with no body', async () => {
    mock.reply(MOCK_ITEMS);
    const result = await proxy.list();
    expect(result).toEqual(MOCK_ITEMS);
    expect(mock.last.headers[':method']).toBe('GET');
    expect(mock.last.headers[':path']).toBe('/items');
    expect(mock.last.payload).toBeUndefined();
  });

  it('retrieve() — path carries the pk', async () => {
    mock.reply(MOCK_ITEM);
    await proxy.retrieve(1);
    expect(mock.last.headers[':path']).toBe('/items/1');
  });

  it('create() — the body is the payload, not a header', async () => {
    mock.reply(MOCK_ITEM);
    await proxy.create(MOCK_ITEM);
    expect(mock.last.headers[':method']).toBe('POST');
    expect(mock.last.payload).toEqual(MOCK_ITEM);
  });

  it('bulkDestroy() — DELETE carries a body, which HTTP makes awkward and muxws does not', async () => {
    mock.reply([]);
    await proxy.bulkDestroy([1, 2]);
    expect(mock.last.headers[':method']).toBe('DELETE');
    expect(mock.last.headers[':path']).toBe('/items/bulk');
    expect(mock.last.payload).toEqual([1, 2]);
  });

  it('opens the stream with end:true — a unary call is complete with its opening frame', async () => {
    mock.reply(MOCK_ITEMS);
    await proxy.list();
    expect(mock.last.end).toBe(true);
  });

  it('omits :query entirely when there is none', async () => {
    mock.reply(MOCK_ITEMS);
    await proxy.list();
    expect(mock.last.headers).not.toHaveProperty(':query');
  });

  it('list() passes filter and sort parameters through as :query', async () => {
    mock.reply(MOCK_ITEMS);
    await proxy.list({ sort: 'id:desc', name: 'Test' });
    expect(mock.last.headers[':query']).toEqual({ sort: 'id:desc', name: 'Test' });
  });

  it('lookup(params) passes the filter through as :query', async () => {
    mock.reply([]);
    await proxy.lookup({ q: 'ap', ids: [1, 2] });
    expect(mock.last.headers[':path']).toBe('/items/lookup');
    expect(mock.last.headers[':query']).toEqual({ q: 'ap', ids: [1, 2] });
  });

  it('listPage() renames the envelope fields the BE sends in snake_case', async () => {
    mock.reply({ results: MOCK_ITEMS, offset: 10, limit: 2, count: 42, has_more: true, has_previous: true });
    const page = await proxy.listPage({ offset: 10, limit: 2 });
    expect(page.results).toEqual(MOCK_ITEMS);
    expect(page.hasMore).toBe(true);
    expect(page.hasPrevious).toBe(true);
    expect(page.count).toBe(42);
    expect(mock.last.headers[':query']).toEqual({ offset: 10, limit: 2 });
  });

  it('listPage() keeps an unknown count as null rather than inventing a zero', async () => {
    mock.reply({ results: MOCK_ITEMS, offset: 0, limit: 2, count: null, has_more: false, has_previous: false });
    const page = await proxy.listPage();
    expect(page.count).toBeNull();
  });

  it('sends configured headers on every call', async () => {
    const withHeaders = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer: mock.peer,
      validateSchema: false,
      headers: { authorization: 'Bearer tok' },
    });
    mock.reply(MOCK_ITEMS);
    await withHeaders.list();
    expect(mock.last.headers.authorization).toBe('Bearer tok');
  });

  it('sends the requested list shape as X-List-Shape, over the configured headers', async () => {
    const withHeaders = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer: mock.peer,
      validateSchema: false,
      headers: { authorization: 'Bearer tok', 'X-List-Shape': 'plain' },
    });
    mock.reply({ results: MOCK_ITEMS, offset: 0, limit: 2, count: 2, has_more: false, has_previous: false });
    await withHeaders.listPage();
    expect(mock.last.headers['X-List-Shape']).toBe('paginated');
    expect(mock.last.headers.authorization).toBe('Bearer tok');
  });

  it('listCursor() rejects an answer in another shape', async () => {
    mock.reply(MOCK_ITEMS);
    await expect(proxy.listCursor()).rejects.toBeInstanceOf(ListShapeMismatchError);
  });

  it('never lets a configured header shadow a pseudo-header', async () => {
    const withHeaders = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer: mock.peer,
      validateSchema: false,
      headers: { ':method': 'DELETE' } as Record<string, string>,
    });
    mock.reply(MOCK_ITEMS);
    await withHeaders.list();
    expect(mock.last.headers[':method']).toBe('GET');
  });
});

describe('MuxwsProxyImpl — responses', () => {
  let mock: ReturnType<typeof makeMockPeer>;
  let proxy: MuxwsProxyImpl<number, Item, 'id'>;

  beforeEach(() => {
    mock = makeMockPeer();
    proxy = new MuxwsProxyImpl({
      basePath: '/items',
      pkFieldName: 'id',
      peer: mock.peer,
      validateSchema: false,
    });
  });

  it('throws on a 4xx, with the axios-shaped status the REST proxy also produces', async () => {
    mock.reply({ detail: 'Not found' }, { ':status': 404 });
    await expect(proxy.retrieve(999)).rejects.toThrow(ViewSetRequestError);
    await expect(proxy.retrieve(999)).rejects.toMatchObject({ response: { status: 404 } });
  });

  it('carries the error body through, so a validation report is still readable', async () => {
    mock.reply({ detail: [{ loc: ['body', 'name'] }] }, { ':status': 422 });
    await expect(proxy.create(MOCK_ITEM)).rejects.toMatchObject({
      response: { status: 422, data: { detail: [{ loc: ['body', 'name'] }] } },
    });
  });

  it('accepts a status sent as a string', async () => {
    mock.reply(null, { ':status': '404' });
    await expect(proxy.retrieve(1)).rejects.toMatchObject({ response: { status: 404 } });
  });

  it('treats a reply with no :status as success', async () => {
    mock.reply(MOCK_ITEMS, null);
    await expect(proxy.list()).resolves.toEqual(MOCK_ITEMS);
  });

  it('exposes non-pseudo reply headers as response headers', async () => {
    mock.reply(null, { ':status': 403, 'x-reason': 'no' });
    await expect(proxy.list()).rejects.toMatchObject({ response: { headers: { 'x-reason': 'no' } } });
  });

  it('propagates a failure it does not repeat rather than dressing it as a status', async () => {
    mock.fail(new Error('stream broke'));
    await expect(proxy.list()).rejects.toThrow('stream broke');
  });
});

describe('MuxwsProxyImpl — timeout', () => {
  it('bounds the wait for the reply headers by timeoutMs', async () => {
    const timedOut = new Error('stream timed out');
    const requested: unknown[] = [];
    const peer: MuxwsPeerLike = {
      open() {
        return {
          // a server that never answers: no headers, and result() rejects once timeoutMs expires
          replyHeadersArrived: new Promise<void>(() => {}),
          replyHeaders: {},
          result: (options) => {
            requested.push(options);
            return Promise.reject(timedOut);
          },
        };
      },
    };
    const proxy = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer,
      timeoutMs: 50,
      validateSchema: false,
    });

    await expect(proxy.list()).rejects.toBe(timedOut);
    expect(requested).toEqual([{ timeoutMs: 50 }]);
  });
});

describe('MuxwsProxyImpl — peer resolution', () => {
  it('accepts a factory, for the usual case where connect() has not resolved yet', async () => {
    const mock = makeMockPeer();
    mock.reply(MOCK_ITEMS);
    const proxy = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer: () => Promise.resolve(mock.peer),
      validateSchema: false,
    });
    await expect(proxy.list()).resolves.toEqual(MOCK_ITEMS);
  });

  it('resolves the factory once and reuses the peer', async () => {
    const mock = makeMockPeer();
    mock.reply(MOCK_ITEMS);
    const factory = vi.fn(() => mock.peer);
    const proxy = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer: factory,
      validateSchema: false,
    });
    await proxy.list();
    await proxy.list();
    expect(factory).toHaveBeenCalledTimes(1);
  });
});

describe('route_muxws', () => {
  it('returns a proxy typed as the declared mixin surface', async () => {
    const mock = makeMockPeer();
    mock.reply(MOCK_ITEMS);
    const items = route_muxws<BulkViewSetMixin<number, Item, 'id'> & LookupMixin>(ItemViewSet, {
      basePath: '/items',
      pkFieldName: 'id',
      peer: mock.peer,
      validateSchema: false,
    });
    await expect(items.list()).resolves.toEqual(MOCK_ITEMS);
  });

  it('fetches the schema over muxws when validation is left on', async () => {
    // Declared, because a ViewSet that claims nothing is not checked and so fetches nothing -
    // what this asserts is that the fetch travels over muxws, not that it happens at all.
    class Declared extends ItemViewSet {
      static declares = [BulkViewSetMixin];
    }
    const mock = makeMockPeer();
    mock.reply({ paths: {} });
    route_muxws<BulkViewSetMixin<number, Item, 'id'>>(Declared, {
      basePath: '/items',
      pkFieldName: 'id',
      peer: mock.peer,
    });
    await vi.waitFor(() => expect(mock.opened.length).toBeGreaterThan(0));
    expect(mock.last.headers[':path']).toBe('/items/schema');
  });

  it('rejects a factory-built ViewSet class', () => {
    // See rest-proxy.spec.ts's equivalent route_rest test for why this must be a compile error, and
    // for why the @ts-expect-error sits on the property access below rather than on this call.
    class ItemApi extends muxwsViewSet<Item>()('id', [BulkViewSetMixin]) {}
    const mock = makeMockPeer();
    const proxy = route_muxws<BulkViewSetMixin<number, Item, 'id'>>(ItemApi, {
      basePath: '/items',
      pkFieldName: 'id',
      peer: mock.peer,
      validateSchema: false,
    });
    // @ts-expect-error route_muxws cannot take a factory-built ViewSet class
    const list = proxy.list;
    expect(typeof list).toBe('function');
  });
});

describe('MuxwsProxyImpl — Idempotency-Key', () => {
  it('opens every stream with a new key and exposes it as call.key', async () => {
    const mock = makeMockPeer();
    mock.reply(MOCK_ITEM);
    const proxy = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer: mock.peer,
      validateSchema: false,
      headers: { 'Idempotency-Key': '"proxy-wide"' },
    });
    const first = proxy.retrieve(1);
    const second = proxy.retrieve(1);
    await Promise.all([first, second]);
    expect(mock.opened[0].headers['Idempotency-Key']).toBe(`"${first.key}"`);
    expect(mock.opened[1].headers['Idempotency-Key']).toBe(`"${second.key}"`);
    expect(first.key).not.toBe(second.key);
  });

  it('sends one Idempotency-Key whatever the spelling of a configured one', async () => {
    const mock = makeMockPeer();
    mock.reply(MOCK_ITEM);
    const proxy = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer: mock.peer,
      validateSchema: false,
      headers: { 'idempotency-key': '"proxy-wide"' },
    });
    const call = proxy.retrieve(1);
    await call;
    const names = Object.keys(mock.last.headers).filter((name) => name.toLowerCase() === 'idempotency-key');
    expect(names).toEqual(['Idempotency-Key']);
    expect(mock.last.headers['Idempotency-Key']).toBe(`"${call.key}"`);
  });

  it('exposes call.key on a custom method of a factory-built ViewSet', async () => {
    class ItemApi extends muxwsViewSet<Item>()('id', [BulkViewSetMixin]) {
      async count(): Promise<number> {
        return this.request<number>('GET', '/count');
      }
    }
    const mock = makeMockPeer();
    mock.reply(3);
    const api = new ItemApi({ basePath: '/items', peer: mock.peer, validateSchema: false });
    const call = api.count() as Promise<number> & { key?: string };
    await expect(call).resolves.toBe(3);
    expect(mock.last.headers['Idempotency-Key']).toBe(`"${call.key}"`);
  });
});

describe('MuxwsProxyImpl — synchronous open', () => {
  it('opens the stream within the call once the peer has resolved', async () => {
    const mock = makeMockPeer();
    mock.reply(MOCK_ITEMS);
    const proxy = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer: () => Promise.resolve(mock.peer),
      validateSchema: false,
    });
    await proxy.list();
    const call = proxy.list();
    expect(mock.opened).toHaveLength(2);
    await call;
  });

  it('opens synchronously on the first call when the peer is given as a value', async () => {
    const mock = makeMockPeer();
    mock.reply(MOCK_ITEMS);
    const proxy = new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer: mock.peer,
      validateSchema: false,
    });
    const call = proxy.list();
    expect(mock.opened).toHaveLength(1);
    await call;
  });
});

describe('MuxwsProxyImpl — repeats', () => {
  /** A muxws-style error: muxws sets `name` on every error class. */
  function muxwsError(name: string): Error {
    const error = new Error(name);
    error.name = name;
    return error;
  }

  /**
   * A peer whose streams fail with the queued errors in order and then answer `body`, and that can
   * be dropped and reconnected the way a muxws Peer reports it.
   */
  function makeFlakyPeer(errors: Error[], body: unknown = MOCK_ITEM) {
    const opened: { headers: Record<string, unknown>; cancelled: boolean }[] = [];
    const reconnectHandlers: ((...args: any[]) => void)[] = [];
    const closeHandlers: ((reason: { willRetry: boolean }) => void)[] = [];
    const state = { isOpen: true, goingAway: false, ambient: undefined as string | undefined };
    const peer: MuxwsPeerLike = {
      get isOpen() {
        return state.isOpen;
      },
      open(_payload, options) {
        if (!state.isOpen) throw muxwsError('ConnectionClosed');
        if (state.goingAway) throw muxwsError('ConnectionGoingAway');
        // an open interceptor that adds a header from state bound only for the caller's synchronous part
        const headers = { ...options?.headers } as Record<string, unknown>;
        if (state.ambient !== undefined) headers['X-Token'] = state.ambient;
        const entry = { headers, cancelled: false };
        opened.push(entry);
        const error = errors[opened.length - 1];
        return {
          headers,
          result: () => (error ? Promise.reject(error) : Promise.resolve(body)),
          replyHeadersArrived: Promise.resolve(),
          replyHeaders: { ':status': 200 },
          cancel: () => {
            entry.cancelled = true;
            return Promise.resolve();
          },
        };
      },
      onReconnect(handler) {
        reconnectHandlers.push(handler);
      },
      onClose(handler) {
        closeHandlers.push(handler);
      },
    };
    return {
      peer,
      opened,
      state,
      drop() {
        state.isOpen = false;
      },
      reconnect() {
        state.isOpen = true;
        for (const handler of reconnectHandlers) handler(1, peer);
      },
      closeForGood() {
        state.isOpen = false;
        for (const handler of closeHandlers) handler({ willRetry: false });
      },
    };
  }

  function proxyOn(peer: MuxwsPeerLike) {
    return new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer,
      validateSchema: false,
    });
  }

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('repeats after ConnectionLost once the peer has reconnected, with the same key', async () => {
    const flaky = makeFlakyPeer([muxwsError('ConnectionLost')]);
    const call = proxyOn(flaky.peer).create(MOCK_ITEM);
    flaky.drop();
    await vi.advanceTimersByTimeAsync(5000);
    expect(flaky.opened).toHaveLength(1);

    flaky.reconnect();
    await vi.advanceTimersByTimeAsync(0);
    await expect(call).resolves.toEqual(MOCK_ITEM);
    expect(flaky.opened).toHaveLength(2);
    expect(flaky.opened[1].headers['Idempotency-Key']).toBe(flaky.opened[0].headers['Idempotency-Key']);
    expect(flaky.opened[0].cancelled).toBe(true);
  });

  it('reports the call and its repeat to onCallActivity under the call key', async () => {
    const events: CallEvent[] = [];
    const stop = onCallActivity((event) => events.push(event));
    try {
      const flaky = makeFlakyPeer([muxwsError('StreamTimeout')]);
      const call = proxyOn(flaky.peer).create(MOCK_ITEM);
      await vi.advanceTimersByTimeAsync(0);
      await call;
      await vi.advanceTimersByTimeAsync(0);
      const { key } = call;
      expect(events).toEqual([
        { type: 'start', key, background: false },
        { type: 'retry', key, failure: 'timeout', attempt: 1, delayMs: 0 },
        { type: 'settle', key, ok: true },
      ]);
    } finally {
      stop();
    }
  });

  it('repeats at once after StreamTimeout', async () => {
    const flaky = makeFlakyPeer([muxwsError('StreamTimeout')]);
    const call = proxyOn(flaky.peer).create(MOCK_ITEM);
    await vi.advanceTimersByTimeAsync(0);
    await expect(call).resolves.toEqual(MOCK_ITEM);
    expect(flaky.opened).toHaveLength(2);
  });

  it('repeats after StreamRefused, after the delay', async () => {
    const flaky = makeFlakyPeer([muxwsError('StreamRefused')]);
    const call = proxyOn(flaky.peer).create(MOCK_ITEM);
    await vi.advanceTimersByTimeAsync(999);
    expect(flaky.opened).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    await expect(call).resolves.toEqual(MOCK_ITEM);
    expect(flaky.opened).toHaveLength(2);
  });

  it('repeats after ConnectionGoingAway, once open() accepts streams again', async () => {
    const flaky = makeFlakyPeer([]);
    flaky.state.goingAway = true;
    const call = proxyOn(flaky.peer).create(MOCK_ITEM);
    await vi.advanceTimersByTimeAsync(1000);
    flaky.state.goingAway = false;
    await vi.advanceTimersByTimeAsync(2000);
    await expect(call).resolves.toEqual(MOCK_ITEM);
    expect(flaky.opened).toHaveLength(1);
  });

  it('opens a repeat with the headers of the first stream, interceptors’ included', async () => {
    const flaky = makeFlakyPeer([muxwsError('StreamRefused')]);
    flaky.state.ambient = 'token';
    const call = proxyOn(flaky.peer).create(MOCK_ITEM);
    flaky.state.ambient = undefined;
    await vi.advanceTimersByTimeAsync(1000);
    await expect(call).resolves.toEqual(MOCK_ITEM);
    expect(flaky.opened[1].headers).toEqual(flaky.opened[0].headers);
    expect(flaky.opened[1].headers['X-Token']).toBe('token');
  });

  it('does not repeat an application answer', async () => {
    const remote = muxwsError('RemoteError');
    const flaky = makeFlakyPeer([remote]);
    await expect(proxyOn(flaky.peer).create(MOCK_ITEM)).rejects.toBe(remote);
    expect(flaky.opened).toHaveLength(1);
  });

  it('stops waiting when the peer closes for good', async () => {
    const lost = muxwsError('ConnectionLost');
    const flaky = makeFlakyPeer([lost]);
    const call = proxyOn(flaky.peer).create(MOCK_ITEM);
    const settled = expect(call).rejects.toMatchObject({ name: 'ConnectionClosed' });
    flaky.drop();
    await vi.advanceTimersByTimeAsync(1000);
    flaky.closeForGood();
    await vi.advanceTimersByTimeAsync(0);
    await settled;
    expect(flaky.opened).toHaveLength(1);
  });

  function proxyFrom(source: () => MuxwsPeerLike | Promise<MuxwsPeerLike>) {
    return new MuxwsProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      peer: source,
      validateSchema: false,
    });
  }

  it('calls a peer source again after its peer has closed for good', async () => {
    const first = makeFlakyPeer([]);
    const second = makeFlakyPeer([]);
    const source = vi.fn().mockReturnValueOnce(first.peer).mockReturnValueOnce(Promise.resolve(second.peer));
    const proxy = proxyFrom(source);
    await proxy.retrieve(1);
    first.drop();
    first.reconnect();
    await proxy.retrieve(1);
    expect(source).toHaveBeenCalledTimes(1);
    first.closeForGood();
    await proxy.retrieve(1);
    await proxy.retrieve(1);
    expect(source).toHaveBeenCalledTimes(2);
    expect([first.opened.length, second.opened.length]).toEqual([2, 2]);
  });

  it('calls a peer source again once its peer reports isClosed', async () => {
    const first = makeFlakyPeer([]);
    const second = makeFlakyPeer([]);
    const source = vi.fn().mockReturnValueOnce(first.peer).mockReturnValueOnce(second.peer);
    const proxy = proxyFrom(source);
    await proxy.retrieve(1);
    Object.defineProperty(first.peer, 'isClosed', { value: true });
    await proxy.retrieve(1);
    expect(source).toHaveBeenCalledTimes(2);
    expect([first.opened.length, second.opened.length]).toEqual([1, 1]);
  });

  it('calls a peer source again after it threw or rejected', async () => {
    const flaky = makeFlakyPeer([]);
    const source = vi
      .fn()
      .mockImplementationOnce(() => {
        throw new Error('not logged in');
      })
      .mockImplementationOnce(() => Promise.reject(new Error('connect failed')))
      .mockReturnValue(flaky.peer);
    const proxy = proxyFrom(source);
    await expect(proxy.retrieve(1)).rejects.toThrow('not logged in');
    await expect(proxy.retrieve(1)).rejects.toThrow('connect failed');
    await expect(proxy.retrieve(1)).resolves.toEqual(MOCK_ITEM);
    expect(source).toHaveBeenCalledTimes(3);
  });

  it('opens the first call synchronously on a peer the source returns synchronously, also a replacement', async () => {
    const first = makeFlakyPeer([]);
    const second = makeFlakyPeer([]);
    const source = vi.fn().mockReturnValueOnce(first.peer).mockReturnValueOnce(second.peer);
    const proxy = proxyFrom(source);
    const call = proxy.retrieve(1);
    expect(first.opened).toHaveLength(1);
    await call;
    first.closeForGood();
    const replaced = proxy.retrieve(1);
    expect(second.opened).toHaveLength(1);
    await replaced;
  });

  it('a repeat waiting for a reconnect rejects on the final close and does not move to the new peer', async () => {
    const first = makeFlakyPeer([muxwsError('ConnectionLost')]);
    const second = makeFlakyPeer([]);
    const source = vi.fn().mockReturnValueOnce(first.peer).mockReturnValueOnce(second.peer);
    const proxy = proxyFrom(source);
    const call = proxy.create(MOCK_ITEM);
    const settled = expect(call).rejects.toMatchObject({
      name: 'ConnectionClosed',
    });
    first.drop();
    await vi.advanceTimersByTimeAsync(1000);
    first.closeForGood();
    await vi.advanceTimersByTimeAsync(0);
    await settled;
    expect(second.opened).toHaveLength(0);
    await proxy.retrieve(1);
    expect(second.opened).toHaveLength(1);
  });

  it('keeps a bare peer after its final close', async () => {
    const flaky = makeFlakyPeer([]);
    const proxy = proxyOn(flaky.peer);
    await proxy.retrieve(1);
    flaky.closeForGood();
    await expect(proxy.retrieve(1)).rejects.toMatchObject({
      name: 'ConnectionClosed',
    });
  });

  it('calls a source that keeps returning the same closed peer on every call', async () => {
    const flaky = makeFlakyPeer([]);
    const source = vi.fn(() => flaky.peer);
    const proxy = proxyFrom(source);
    await proxy.retrieve(1);
    flaky.closeForGood();
    await expect(proxy.retrieve(1)).rejects.toMatchObject({
      name: 'ConnectionClosed',
    });
    await expect(proxy.retrieve(1)).rejects.toMatchObject({
      name: 'ConnectionClosed',
    });
    expect(source).toHaveBeenCalledTimes(3);
  });

  it('watch() follows the replacement peer to its event client', async () => {
    const first = makeFlakyPeer([]);
    const second = makeFlakyPeer([], { rev: 0 });
    const source = vi.fn().mockReturnValueOnce(first.peer).mockReturnValueOnce(second.peer);
    const proxy = proxyFrom(source);
    await proxy.retrieve(1);
    first.closeForGood();
    const events = createEventClient({
      basePath: '/events',
      peer: second.peer,
    });
    try {
      const subscription = proxy.watch({ onChange() {}, onResync() {} });
      await subscription.ready;
      expect(second.opened.map((stream) => stream.headers[':path'])).toEqual(['/events/subscribe']);
    } finally {
      events.close();
    }
  });
});
