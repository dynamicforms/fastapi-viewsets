/**
 * Unit tests for muxws-proxy.ts
 *
 * The peer is faked rather than connected: what matters here is the envelope the proxy puts on
 * the open frame and what it makes of the reply, not muxws' own behaviour, which has its own
 * conformance suite.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import { BulkViewSetMixin, LookupMixin } from './mixins';
import { type MuxwsPeerLike, MuxwsProxyImpl, route_muxws } from './muxws-proxy';
import { ViewSetRequestError } from './proxy-base';

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
  let nextTrailers: Record<string, unknown> | null = { ':status': 200 };
  let nextError: Error | null = null;

  const peer: MuxwsPeerLike = {
    open(payload, options) {
      opened.push({ payload, headers: (options?.headers ?? {}) as Record<string, unknown>, end: options?.end });
      const body = nextBody;
      const trailers = nextTrailers;
      const error = nextError;
      return {
        result: () => (error ? Promise.reject(error) : Promise.resolve(body)),
        closed: Promise.resolve(),
        trailers,
      };
    },
  };

  return {
    peer,
    opened,
    reply(body: unknown, trailers: Record<string, unknown> | null = { ':status': 200 }) {
      nextBody = body;
      nextTrailers = trailers;
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

  it('exposes non-pseudo trailers as response headers', async () => {
    mock.reply(null, { ':status': 403, 'x-reason': 'no' });
    await expect(proxy.list()).rejects.toMatchObject({ response: { headers: { 'x-reason': 'no' } } });
  });

  it('propagates a transport failure rather than dressing it as a status', async () => {
    // Nothing survives a muxws reconnect: an in-flight call fails with ConnectionLost, and that
    // is the caller's decision to retry, not ours to hide.
    mock.fail(new Error('ConnectionLost'));
    await expect(proxy.list()).rejects.toThrow('ConnectionLost');
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
    const mock = makeMockPeer();
    mock.reply({ paths: {} });
    route_muxws<BulkViewSetMixin<number, Item, 'id'>>(ItemViewSet, {
      basePath: '/items',
      pkFieldName: 'id',
      peer: mock.peer,
    });
    await vi.waitFor(() => expect(mock.opened.length).toBeGreaterThan(0));
    expect(mock.last.headers[':path']).toBe('/items/schema');
  });
});
