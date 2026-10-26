/**
 * Unit tests for rest-proxy.ts
 *
 * Tests cover:
 * 1. How to declare a ViewSet class and pass it to route_rest
 * 2. How to instantiate a REST proxy with route_rest
 * 3. That the proxy calls the correct HTTP methods on the correct URLs
 */

import axios, { AxiosError, AxiosHeaders, CanceledError, type InternalAxiosRequestConfig } from 'axios';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { type CallEvent, onCallActivity } from './call';
import {
  BulkViewSetMixin,
  CursorListMixin,
  ListMixin,
  LookupMixin,
  PaginatedListMixin,
  ReadOnlyViewSetMixin,
  RetrieveMixin,
  ViewSetMixin,
} from './mixins';
import { ListShapeMismatchError } from './proxy-base';
import { RestProxyImpl, route_rest } from './rest-proxy';
import { restViewSet } from './viewset';

// ---------------------------------------------------------------------------
// Helper types and fixture data
// ---------------------------------------------------------------------------

interface Item {
  id: number;
  name: string;
}

// ViewSet classes — mirror BE declarations, e.g.:
//   class ItemViewSet(BulkViewSetMixin[int, Item], LookupMixin): ...
abstract class ItemReadOnlyViewSet extends ReadOnlyViewSetMixin<number, Item> {}
abstract class ItemViewSet extends ViewSetMixin<number, Item, 'id'> {}

const MOCK_ITEM: Item = { id: 1, name: 'Test item' };
const MOCK_ITEMS: Item[] = [MOCK_ITEM, { id: 2, name: 'Second item' }];

// ---------------------------------------------------------------------------
// Setup: mock axios instance
// ---------------------------------------------------------------------------

/** The config every REST attempt carries: the call's own, plus the key, the timeout and the abort signal. */
function sent(config: { params?: unknown; headers?: Record<string, string>; data?: unknown } = {}) {
  return {
    ...config,
    ...(config.params !== undefined ? { paramsSerializer: { indexes: null } } : {}),
    headers: { ...config.headers, 'Idempotency-Key': expect.stringMatching(/^"[0-9a-f-]{36}"$/) },
    timeout: 30000,
    signal: expect.any(AbortSignal),
  };
}

function makeMockAxios() {
  return {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  };
}

// ---------------------------------------------------------------------------
// 1. RestProxyImpl — direct instance
// ---------------------------------------------------------------------------

describe('RestProxyImpl — direct instance', () => {
  let http: ReturnType<typeof makeMockAxios>;
  let proxy: RestProxyImpl<number, Item, 'id'>;

  beforeEach(() => {
    http = makeMockAxios();
    proxy = new RestProxyImpl({
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
    });
  });

  it('list() — GET /items', async () => {
    http.get.mockResolvedValue({ data: MOCK_ITEMS });
    const result = await proxy.list();
    expect(http.get).toHaveBeenCalledWith('/items', sent({ headers: { 'X-List-Shape': 'plain' } }));
    expect(result).toEqual(MOCK_ITEMS);
  });

  it('retrieve() — GET /items/1', async () => {
    http.get.mockResolvedValue({ data: MOCK_ITEM });
    const result = await proxy.retrieve(1);
    expect(http.get).toHaveBeenCalledWith('/items/1', sent());
    expect(result).toEqual(MOCK_ITEM);
  });

  it('create() — POST /items', async () => {
    http.post.mockResolvedValue({ data: MOCK_ITEM });
    const result = await proxy.create(MOCK_ITEM);
    expect(http.post).toHaveBeenCalledWith('/items', MOCK_ITEM, sent());
    expect(result).toEqual(MOCK_ITEM);
  });

  it('update() — PUT /items/1', async () => {
    const updated = { ...MOCK_ITEM, name: 'Updated' };
    http.put.mockResolvedValue({ data: updated });
    const result = await proxy.update(1, updated);
    expect(http.put).toHaveBeenCalledWith('/items/1', updated, sent());
    expect(result).toEqual(updated);
  });

  it('partialUpdate() — PATCH /items/1', async () => {
    const patch = { name: 'Patched' };
    http.patch.mockResolvedValue({ data: { ...MOCK_ITEM, ...patch } });
    const result = await proxy.partialUpdate(1, patch);
    expect(http.patch).toHaveBeenCalledWith('/items/1', patch, sent());
    expect(result).toEqual({ ...MOCK_ITEM, ...patch });
  });

  it('destroy() — DELETE /items/1', async () => {
    http.delete.mockResolvedValue({ data: { deleted: 1 } });
    const result = await proxy.destroy(1);
    expect(http.delete).toHaveBeenCalledWith('/items/1', sent());
    expect(result).toEqual({ deleted: 1 });
  });

  it('bulkCreate() — POST /items/bulk', async () => {
    http.post.mockResolvedValue({ data: MOCK_ITEMS });
    const result = await proxy.bulkCreate(MOCK_ITEMS);
    expect(http.post).toHaveBeenCalledWith('/items/bulk', MOCK_ITEMS, sent());
    expect(result).toEqual(MOCK_ITEMS);
  });

  it('bulkUpdate() — PUT /items/bulk', async () => {
    const records = { 1: MOCK_ITEM };
    http.put.mockResolvedValue({ data: MOCK_ITEMS });
    const result = await proxy.bulkUpdate(records);
    expect(http.put).toHaveBeenCalledWith('/items/bulk', records, sent());
    expect(result).toEqual(MOCK_ITEMS);
  });

  it('bulkPartialUpdate() — PATCH /items/bulk', async () => {
    const records = { 1: { name: 'Patched' } };
    http.patch.mockResolvedValue({ data: MOCK_ITEMS });
    const result = await proxy.bulkPartialUpdate(records);
    expect(http.patch).toHaveBeenCalledWith('/items/bulk', records, sent());
    expect(result).toEqual(MOCK_ITEMS);
  });

  it('bulkDestroy() — DELETE /items/bulk', async () => {
    http.delete.mockResolvedValue({ data: [{ deleted: 1 }] });
    const result = await proxy.bulkDestroy([1, 2]);
    expect(http.delete).toHaveBeenCalledWith('/items/bulk', sent({ data: [1, 2] }));
    expect(result).toEqual([{ deleted: 1 }]);
  });

  it('listPage() requests the paginated shape and passes offset/limit as params', async () => {
    http.get.mockResolvedValue({
      data: { results: MOCK_ITEMS, offset: 10, limit: 2, count: 42, has_more: true, has_previous: true },
    });
    const page = await proxy.listPage({ offset: 10, limit: 2 });
    expect(http.get).toHaveBeenCalledWith(
      '/items',
      sent({ params: { offset: 10, limit: 2 }, headers: { 'X-List-Shape': 'paginated' } }),
    );
    expect(page).toEqual({ results: MOCK_ITEMS, offset: 10, limit: 2, count: 42, hasMore: true, hasPrevious: true });
  });

  it('listCursor() requests the cursor shape', async () => {
    const envelope = {
      results: MOCK_ITEMS,
      limit: 2,
      has_more: true,
      has_previous: false,
      next: 'n',
      previous: null,
      first: 'f',
      last: 'l',
    };
    http.get.mockResolvedValue({ data: envelope });
    const page = await proxy.listCursor({ limit: 2 });
    expect(http.get).toHaveBeenCalledWith(
      '/items',
      sent({ params: { limit: 2 }, headers: { 'X-List-Shape': 'cursor' } }),
    );
    expect(page.next).toBe('n');
    expect(page.hasMore).toBe(true);
  });

  it('list(), listPage() and listCursor() send listType as X-List-Type', async () => {
    const types = { 'X-List-Type': 'compact' };
    http.get.mockResolvedValueOnce({ data: MOCK_ITEMS });
    await proxy.list({ sort: 'name:asc' }, { listType: 'compact' });
    expect(http.get).toHaveBeenLastCalledWith(
      '/items',
      sent({ params: { sort: 'name:asc' }, headers: { 'X-List-Shape': 'plain', ...types } }),
    );
    http.get.mockResolvedValueOnce({
      data: { results: MOCK_ITEMS, offset: 0, limit: 2, count: 2, has_more: false, has_previous: false },
    });
    await proxy.listPage({ limit: 2 }, { listType: 'compact' });
    expect(http.get).toHaveBeenLastCalledWith(
      '/items',
      sent({ params: { limit: 2 }, headers: { 'X-List-Shape': 'paginated', ...types } }),
    );
    http.get.mockResolvedValueOnce({
      data: { results: [], limit: 2, has_more: false, has_previous: false, next: null, previous: null },
    });
    await proxy.listCursor(undefined, { listType: 'compact' });
    expect(http.get).toHaveBeenLastCalledWith('/items', sent({ headers: { 'X-List-Shape': 'cursor', ...types } }));
  });

  it('sends no X-List-Type without listType', async () => {
    http.get.mockResolvedValue({ data: MOCK_ITEMS });
    await proxy.list(undefined, {});
    expect(http.get).toHaveBeenCalledWith('/items', sent({ headers: { 'X-List-Shape': 'plain' } }));
  });

  it('listPage() rejects a cursor envelope from a viewset that offers only cursor paging', async () => {
    http.get.mockResolvedValue({
      data: { results: [], limit: 2, has_more: false, has_previous: false, next: null, previous: null },
    });
    const error = await proxy.listPage().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ListShapeMismatchError);
    expect(error).toMatchObject({ expected: 'paginated', received: 'cursor' });
  });

  it('list() rejects a paged envelope', async () => {
    http.get.mockResolvedValue({ data: { results: [], offset: 0, limit: 2, count: 0 } });
    await expect(proxy.list()).rejects.toMatchObject({ expected: 'plain', received: 'paginated' });
  });

  it('listCursor() reports a body matching no envelope as unknown', async () => {
    http.get.mockResolvedValue({ data: { detail: 'x' } });
    await expect(proxy.listCursor()).rejects.toMatchObject({ expected: 'cursor', received: 'unknown' });
  });

  it('lookup() — GET /items/lookup', async () => {
    const lookupItems = [{ group: null, pk: 1, title: 'Test item', icon: null }];
    http.get.mockResolvedValue({ data: lookupItems });
    const result = await proxy.lookup();
    expect(http.get).toHaveBeenCalledWith('/items/lookup', sent());
    expect(result).toEqual(lookupItems);
  });

  it('lookup(params) — sends the filter as query parameters', async () => {
    http.get.mockResolvedValue({ data: [] });
    await proxy.lookup({ q: 'ap', ids: [1, 2] });
    expect(http.get).toHaveBeenCalledWith('/items/lookup', sent({ params: { q: 'ap', ids: [1, 2] } }));
  });

  it('sends an array query parameter as a repeated key, the form FastAPI reads into a list', async () => {
    let url = '';
    const real = axios.create({
      adapter: async (config) => {
        url = axios.getUri(config);
        return { data: [], status: 200, statusText: 'OK', headers: {}, config };
      },
    });
    class Tags extends restViewSet<Item>()('id', [LookupMixin]) {}
    await new Tags({ basePath: '/tags', validateSchema: false, axiosInstance: real }).lookup({ q: 'a', ids: [1, 2] });
    expect(url).toBe('/tags/lookup?q=a&ids=1&ids=2');
  });

  it('sends an array list filter as a repeated key on every list shape', async () => {
    const urls: string[] = [];
    const real = axios.create({
      adapter: async (config) => {
        urls.push(axios.getUri(config));
        const shape = AxiosHeaders.from(config.headers).get('X-List-Shape');
        const data = shape === 'paginated' ? { results: [], offset: 0, limit: 10, count: 0 } : [];
        return { data, status: 200, statusText: 'OK', headers: {}, config };
      },
    });
    class Tracks extends restViewSet<Item>()('id', [PaginatedListMixin, ListMixin]) {}
    const tracks = new Tracks({ basePath: '/tracks', validateSchema: false, axiosInstance: real });
    await tracks.list({ tags: ['bebop', 'ballad'], sort: 'id:asc' });
    await tracks.listPage({ tags: ['modal'], limit: 10 });
    expect(urls).toEqual(['/tracks?tags=bebop&tags=ballad&sort=id:asc', '/tracks?tags=modal&limit=10']);
  });

  it('basePath trailing slash is stripped', () => {
    const p = new RestProxyImpl({
      basePath: '/items/',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
    });
    // @ts-expect-error — accessing protected field for testing
    expect(p.basePath).toBe('/items');
  });
});

// ---------------------------------------------------------------------------
// 2. route_rest — factory function with separate arguments
// ---------------------------------------------------------------------------

describe('route_rest — factory with separate arguments', () => {
  let http: ReturnType<typeof makeMockAxios>;

  beforeEach(() => {
    http = makeMockAxios();
  });

  it('returns proxy with list() and retrieve() for ReadOnlyViewSet', async () => {
    const proxy = route_rest<ReadOnlyViewSetMixin<number, Item>>(
      ItemReadOnlyViewSet,
      '/items',
      'id',
      http as unknown as typeof axios,
    );

    http.get.mockResolvedValue({ data: MOCK_ITEMS });
    const items = await proxy.list();
    expect(items).toEqual(MOCK_ITEMS);

    http.get.mockResolvedValue({ data: MOCK_ITEM });
    const item = await proxy.retrieve(1);
    expect(item).toEqual(MOCK_ITEM);
  });

  it('returns proxy with all CRUD methods for ViewSetMixin', async () => {
    const proxy = route_rest<ViewSetMixin<number, Item, 'id'>>(ItemViewSet, '/items', 'id', http as any);

    http.post.mockResolvedValue({ data: MOCK_ITEM });
    const created = await proxy.create(MOCK_ITEM);
    expect(created).toEqual(MOCK_ITEM);

    http.put.mockResolvedValue({ data: MOCK_ITEM });
    const updated = await proxy.update(1, MOCK_ITEM);
    expect(updated).toEqual(MOCK_ITEM);

    http.delete.mockResolvedValue({ data: {} });
    await proxy.destroy(1);
    expect(http.delete).toHaveBeenCalledWith('/items/1', sent());
  });

  it('rejects a factory-built ViewSet class', () => {
    // A factory-built class's own methods (added past what route_rest can type through M) would be
    // undefined at runtime if this compiled: route_rest builds a bare RestProxyImpl and discards
    // the class itself. See viewset.spec.ts for the equivalent muxwsViewSet-side coverage note.
    //
    // The reject overload's return type - a string literal, not RestProxy<M> - is what makes the
    // call itself an error; a call whose result is never used produces no diagnostic either way, so
    // the @ts-expect-error has to sit on the line that tries to use it, not on the call.
    class ItemApi extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin]) {}
    const proxy = route_rest<ReadOnlyViewSetMixin<number, Item>>(ItemApi, '/items', 'id');
    // @ts-expect-error route_rest cannot take a factory-built ViewSet class - property access
    // rather than a call, so a real HTTP request is never attempted even though this is type-only
    const list = proxy.list;
    expect(typeof list).toBe('function');
  });
});

// ---------------------------------------------------------------------------
// 3. route_rest — factory function with options object
// ---------------------------------------------------------------------------

describe('route_rest — factory with options object', () => {
  let http: ReturnType<typeof makeMockAxios>;

  beforeEach(() => {
    http = makeMockAxios();
  });

  it('returns proxy with options object', async () => {
    const proxy = route_rest<ListMixin<Item> & RetrieveMixin<number, Item>>(ItemReadOnlyViewSet, {
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
    });

    http.get.mockResolvedValue({ data: MOCK_ITEMS });
    const items = await proxy.list();
    expect(items).toEqual(MOCK_ITEMS);
  });
});

// ---------------------------------------------------------------------------
// 4. route_rest — BulkViewSetMixin + LookupMixin
// ---------------------------------------------------------------------------

describe('route_rest — BulkViewSetMixin & LookupMixin', () => {
  let http: ReturnType<typeof makeMockAxios>;
  let proxy: BulkViewSetMixin<number, Item, 'id'> & LookupMixin;

  beforeEach(() => {
    http = makeMockAxios();
    proxy = new RestProxyImpl<number, Item, 'id'>({
      axiosInstance: http as any,
      basePath: '/items',
      pkFieldName: 'id',
    });
  });

  it('list()', async () => {
    http.get.mockResolvedValue({ data: MOCK_ITEMS });
    expect(await proxy.list()).toEqual(MOCK_ITEMS);
  });

  it('retrieve()', async () => {
    http.get.mockResolvedValue({ data: MOCK_ITEM });
    expect(await proxy.retrieve(1)).toEqual(MOCK_ITEM);
  });

  it('create()', async () => {
    http.post.mockResolvedValue({ data: MOCK_ITEM });
    expect(await proxy.create(MOCK_ITEM)).toEqual(MOCK_ITEM);
  });

  it('bulkCreate()', async () => {
    http.post.mockResolvedValue({ data: MOCK_ITEMS });
    expect(await proxy.bulkCreate(MOCK_ITEMS)).toEqual(MOCK_ITEMS);
  });

  it('update()', async () => {
    http.put.mockResolvedValue({ data: MOCK_ITEM });
    expect(await proxy.update(1, MOCK_ITEM)).toEqual(MOCK_ITEM);
  });

  it('partialUpdate()', async () => {
    http.patch.mockResolvedValue({ data: MOCK_ITEM });
    expect(await proxy.partialUpdate(1, { name: 'x' })).toEqual(MOCK_ITEM);
  });

  it('bulkUpdate()', async () => {
    http.put.mockResolvedValue({ data: MOCK_ITEMS });
    expect(await proxy.bulkUpdate({ 1: MOCK_ITEM })).toEqual(MOCK_ITEMS);
  });

  it('bulkPartialUpdate()', async () => {
    http.patch.mockResolvedValue({ data: MOCK_ITEMS });
    expect(await proxy.bulkPartialUpdate({ 1: { name: 'x' } })).toEqual(MOCK_ITEMS);
  });

  it('destroy()', async () => {
    http.delete.mockResolvedValue({ data: {} });
    await proxy.destroy(1);
    expect(http.delete).toHaveBeenCalledWith('/items/1', sent());
  });

  it('bulkDestroy()', async () => {
    http.delete.mockResolvedValue({ data: [] });
    await proxy.bulkDestroy([1, 2]);
    expect(http.delete).toHaveBeenCalledWith('/items/bulk', sent({ data: [1, 2] }));
  });

  it('lookup()', async () => {
    const items = [{ group: null, pk: 1, title: 'Test', icon: null }];
    http.get.mockResolvedValue({ data: items });
    expect(await proxy.lookup()).toEqual(items);
  });
});

// ---------------------------------------------------------------------------
// 5. Custom endpoints — extending RestProxyImpl
// (as described in docs/guide/custom-endpoints.md)
// ---------------------------------------------------------------------------

interface CloneRequest {
  source_id: number;
  new_name: string;
}

class ItemApi extends RestProxyImpl<number, Item, 'id'> {
  async search(q: string): Promise<Item[]> {
    const res = await this.http.get<Item[]>(`${this.basePath}/search`, { params: { q } });
    return res.data;
  }

  async clone(body: CloneRequest): Promise<Item> {
    const res = await this.http.post<Item>(`${this.basePath}/clone`, body);
    return res.data;
  }
}

describe('custom endpoints — extending RestProxyImpl', () => {
  let http: ReturnType<typeof makeMockAxios>;
  let api: ItemApi;

  beforeEach(() => {
    http = makeMockAxios();
    api = new ItemApi({ axiosInstance: http as any, basePath: '/items', pkFieldName: 'id' });
  });

  it('search() calls GET /items/search with query param', async () => {
    http.get.mockResolvedValue({ data: [MOCK_ITEM] });
    const result = await api.search('test');
    expect(http.get).toHaveBeenCalledWith('/items/search', { params: { q: 'test' } });
    expect(result).toEqual([MOCK_ITEM]);
  });

  it('search() returns empty array when no results', async () => {
    http.get.mockResolvedValue({ data: [] });
    const result = await api.search('nonexistent');
    expect(result).toEqual([]);
  });

  it('clone() calls POST /items/clone with request body', async () => {
    const cloned: Item = { id: 3, name: 'Widget copy' };
    http.post.mockResolvedValue({ data: cloned });
    const body: CloneRequest = { source_id: 1, new_name: 'Widget copy' };
    const result = await api.clone(body);
    expect(http.post).toHaveBeenCalledWith('/items/clone', body);
    expect(result).toEqual(cloned);
  });

  it('standard mixin methods still work on extended class', async () => {
    http.get.mockResolvedValue({ data: [MOCK_ITEM] });
    const items = await api.list();
    expect(http.get).toHaveBeenCalledWith('/items', sent({ headers: { 'X-List-Shape': 'plain' } }));
    expect(items).toEqual([MOCK_ITEM]);
  });

  it('retrieve() still works on extended class', async () => {
    http.get.mockResolvedValue({ data: MOCK_ITEM });
    const item = await api.retrieve(1);
    expect(http.get).toHaveBeenCalledWith('/items/1', sent());
    expect(item).toEqual(MOCK_ITEM);
  });

  it('this.basePath is accessible and correct', () => {
    expect((api as any).basePath).toBe('/items');
  });

  it('this.http is the injected axios instance', () => {
    expect((api as any).http).toBe(http);
  });
});

// ---------------------------------------------------------------------------
// 6. RestProxyImpl — default axios instance
// ---------------------------------------------------------------------------

describe('RestProxyImpl — default axios instance', () => {
  it('uses global axios when axiosInstance is not provided', () => {
    const proxy = new RestProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
    });
    expect((proxy as any).http).toBe(axios);
  });
});

// ---------------------------------------------------------------------------
// 7. Schema validation — validateAgainstSchema()
// ---------------------------------------------------------------------------

/** Full schema for an endpoint that exposes BulkViewSetMixin + LookupMixin. */
const FULL_SCHEMA = {
  paths: {
    '/items': { get: {}, post: {} },
    '/items/{pk}': { get: {}, put: {}, patch: {}, delete: {} },
    '/items/bulk': { post: {}, put: {}, patch: {}, delete: {} },
    '/items/lookup': { get: {} },
    '/items/schema': { get: {} },
  },
};

/** Schema for a ReadOnly endpoint (list + retrieve only). */
const READONLY_SCHEMA = {
  paths: {
    '/items': { get: {} },
    '/items/{pk}': { get: {} },
  },
};

describe('schema validation — validateAgainstSchema()', () => {
  let http: ReturnType<typeof makeMockAxios>;
  let warnSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    http = makeMockAxios();
    warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
  });

  afterEach(() => {
    warnSpy.mockRestore();
  });

  function mockSchema(schema: object) {
    http.get.mockImplementation((url: string) =>
      url === '/items/schema' ? Promise.resolve({ data: schema }) : Promise.resolve({ data: [] }),
    );
  }

  /** A ViewSet declaring the given mixins, the way a consumer writes one. */
  function viewSetDeclaring(declares: readonly { actions: readonly string[] }[], extra: object = {}) {
    class Declared extends RestProxyImpl<number, Item, 'id'> {
      static declares = declares;
    }
    Object.assign(Declared.prototype, extra);
    return Declared;
  }

  async function createAndValidate(
    schema?: object,
    declares: readonly { actions: readonly string[] }[] = [BulkViewSetMixin, LookupMixin],
    extra: object = {},
  ) {
    if (schema !== undefined) mockSchema(schema);
    const proxy = new (viewSetDeclaring(declares, extra))({
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
    });
    // Wait for the constructor's fire-and-forget to complete, then reset the spy
    // so that the explicit call below is the only one captured in assertions.
    await new Promise((r) => setTimeout(r, 0));
    warnSpy.mockClear();
    await (proxy as any).validateAgainstSchema();
    return proxy;
  }

  it('runs from the constructor, not only when called by hand', async () => {
    // The check lives on the shared base class but reads `this.http`, which the subclass
    // constructor assigns after super() returns, so it runs from the subclass constructor.
    // It swallows its own errors: started from the base constructor it would do nothing.
    mockSchema(FULL_SCHEMA);
    new (viewSetDeclaring([BulkViewSetMixin, LookupMixin]))({
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
    });
    await vi.waitFor(() => expect(http.get).toHaveBeenCalledWith('/items/schema', sent()));
  });

  it('no warnings when BE matches what the ViewSet declared', async () => {
    await createAndValidate(FULL_SCHEMA);
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('a ViewSet declaring one mixin says nothing about actions it never claimed', async () => {
    // The check compares the BE against the actions the ViewSet declared, not against the methods
    // the proxy has: every standard method exists on the proxy whichever mixins are declared.
    await createAndValidate({ paths: { '/items': { get: {} }, '/items/schema': { get: {} } } }, [CursorListMixin]);
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('a ViewSet that declares nothing is not checked at all, and costs no request', async () => {
    mockSchema(FULL_SCHEMA);
    const proxy = new RestProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
    });
    await new Promise((r) => setTimeout(r, 0));
    await (proxy as any).validateAgainstSchema();
    expect(warnSpy).not.toHaveBeenCalled();
    expect(http.get.mock.calls.map(([url]) => url)).not.toContain('/items/schema');
  });

  it('warns for each declared action the BE viewset does not serve', async () => {
    await createAndValidate(READONLY_SCHEMA);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    const msg = warnSpy.mock.calls[0][0] as string;
    expect(msg).toContain('[ViewSet /items]');
    // list and retrieve are served → not warned about
    expect(msg).not.toContain("declares 'list'");
    expect(msg).not.toContain("declares 'retrieve'");
    for (const action of [
      'create',
      'update',
      'partialUpdate',
      'destroy',
      'bulkCreate',
      'bulkUpdate',
      'bulkPartialUpdate',
      'bulkDestroy',
      'lookup',
    ]) {
      expect(msg).toContain(`declares '${action}'`);
    }
  });

  it('warns when the BE serves a standard endpoint the ViewSet never declared', async () => {
    await createAndValidate(FULL_SCHEMA, [CursorListMixin]);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    const msg = warnSpy.mock.calls[0][0] as string;
    expect(msg).toContain("BE serves 'POST /items'");
    expect(msg).toContain("BE serves 'GET /items/lookup'");
    // GET /items is satisfied by listCursor — one endpoint answers in whichever shape was declared
    expect(msg).not.toContain("BE serves 'GET /items'\n");
  });

  it('a declared list shape is satisfied by the single GET the BE serves', async () => {
    // cursor, page and plain are three shapes of one endpoint, not three endpoints
    for (const shape of [CursorListMixin, PaginatedListMixin, ListMixin]) {
      warnSpy.mockClear();
      await createAndValidate({ paths: { '/items': { get: {} } } }, [shape]);
      expect(warnSpy).not.toHaveBeenCalled();
    }
  });

  it('checks custom endpoints against methods the ViewSet actually wrote', async () => {
    // The base implements no custom actions, so a method named after an endpoint's
    // x-viewset-action is one the ViewSet defines.
    await createAndValidate(
      { paths: { ...FULL_SCHEMA.paths, '/items/export': { get: { 'x-viewset-action': 'export' } } } },
      [BulkViewSetMixin, LookupMixin],
      { export: () => Promise.resolve(null) },
    );
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('warns about a custom BE endpoint the ViewSet has no method for', async () => {
    await createAndValidate({
      paths: { ...FULL_SCHEMA.paths, '/items/import': { post: { 'x-viewset-action': 'import' } } },
    });
    expect(warnSpy).toHaveBeenCalledTimes(1);
    const msg = warnSpy.mock.calls[0][0] as string;
    expect(msg).toContain("BE serves 'POST /items/import'");
    expect(msg).toContain("no 'import()' method");
  });

  it('no warnings when schema fetch fails (non-critical)', async () => {
    http.get.mockRejectedValue(new Error('Network error'));
    await createAndValidate();
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('skips the /schema endpoint itself from comparison', async () => {
    // FULL_SCHEMA already includes /items/schema — ensure it does not produce a warning
    await createAndValidate(FULL_SCHEMA);
    expect(warnSpy).not.toHaveBeenCalled();
  });

  /** Validates a ViewSet routed at `basePath` against a document whose paths lack its prefix. */
  async function validateRoutedAt(
    basePath: string,
    schema: object,
    declares: readonly { actions: readonly string[] }[] = [BulkViewSetMixin, LookupMixin],
  ) {
    http.get.mockImplementation((url: string) =>
      url === `${basePath}/schema` ? Promise.resolve({ data: schema }) : Promise.resolve({ data: [] }),
    );
    const proxy = new (viewSetDeclaring(declares))({
      basePath,
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
    });
    await new Promise((r) => setTimeout(r, 0));
    warnSpy.mockClear();
    await (proxy as any).validateAgainstSchema();
  }

  it('takes the base from the document under an include_router prefix', async () => {
    await validateRoutedAt('/api/v1/items', FULL_SCHEMA);
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('still reports a mismatch under an include_router prefix', async () => {
    await validateRoutedAt('/api/v1/items', FULL_SCHEMA, [CursorListMixin]);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy.mock.calls[0][0]).toContain("BE serves 'GET /items/{pk}'");
  });

  it('takes the base from the document when the base path has a path parameter', async () => {
    const paths = Object.fromEntries(
      Object.entries(FULL_SCHEMA.paths).map(([path, item]) => [`/orgs/{org}${path}`, item]),
    );
    await validateRoutedAt('/api/orgs/acme/items', { paths });
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('ignores non-HTTP-method keys in path items (parameters, summary, etc.)', async () => {
    await createAndValidate({
      paths: {
        ...FULL_SCHEMA.paths,
        '/items': { get: {}, post: {}, parameters: [], summary: 'Items endpoint' },
      },
    });
    expect(warnSpy).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 8. Idempotency-Key and repeats
// ---------------------------------------------------------------------------

describe('defaultSort()', () => {
  let http: ReturnType<typeof makeMockAxios>;

  beforeEach(() => {
    http = makeMockAxios();
  });

  function schemaWithSort(sortDefault?: string, base = '/items') {
    const sort = { name: 'sort', in: 'query', schema: sortDefault === undefined ? {} : { default: sortDefault } };
    return { paths: { [base]: { get: { parameters: [sort] } }, [`${base}/schema`]: { get: {} } } };
  }

  function makeApi(declares: readonly { actions: readonly string[] }[] = [ReadOnlyViewSetMixin]) {
    class Api extends RestProxyImpl<number, Item, 'id'> {
      static declares = declares;
    }
    return new Api({ basePath: '/items', pkFieldName: 'id', axiosInstance: http as unknown as typeof axios });
  }

  it("parses the list endpoint's sort parameter default", async () => {
    http.get.mockResolvedValue({ data: schemaWithSort('year:desc,title') });
    await expect(makeApi().defaultSort()).resolves.toEqual([
      { columnName: 'year', direction: 'desc' },
      { columnName: 'title', direction: 'asc' },
    ]);
  });

  it('is [] when the BE viewset declares no default_sort', async () => {
    http.get.mockResolvedValue({ data: schemaWithSort() });
    await expect(makeApi().defaultSort()).resolves.toEqual([]);
  });

  it('reads the base path from the schema document, not from basePath', async () => {
    http.get.mockResolvedValue({ data: schemaWithSort('title:asc', '/catalogue/items') });
    await expect(makeApi().defaultSort()).resolves.toEqual([{ columnName: 'title', direction: 'asc' }]);
  });

  it('finds the collection of a viewset routed at base_path ""', async () => {
    const schema = schemaWithSort('id:desc');
    http.get.mockResolvedValue({ data: { paths: { '/': schema.paths['/items'], '/schema': { get: {} } } } });
    await expect(makeApi().defaultSort()).resolves.toEqual([{ columnName: 'id', direction: 'desc' }]);
  });

  it("issues its own /schema request when the schema check's fails", async () => {
    http.get.mockRejectedValueOnce(new Error('offline')).mockResolvedValue({ data: schemaWithSort('title:asc') });
    const api = makeApi();
    await expect(api.defaultSort()).resolves.toEqual([{ columnName: 'title', direction: 'asc' }]);
    expect(http.get.mock.calls.filter(([url]) => url === '/items/schema')).toHaveLength(2);
  });

  it('shares one /schema request with the schema check and with later calls', async () => {
    http.get.mockResolvedValue({ data: schemaWithSort('title:asc') });
    const api = makeApi();
    await api.defaultSort();
    await api.defaultSort();
    expect(http.get.mock.calls.filter(([url]) => url === '/items/schema')).toHaveLength(1);
  });

  it('is on a route_rest proxy whose mixin interface has a list action', async () => {
    http.get.mockResolvedValue({ data: schemaWithSort('title:asc') });
    class Plain extends RestProxyImpl<number, Item, 'id'> {}
    const api = route_rest<ListMixin<Item>>(Plain, {
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
    });
    await expect(api.defaultSort()).resolves.toEqual([{ columnName: 'title', direction: 'asc' }]);
    const lookupOnly = route_rest<LookupMixin>(Plain, {
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
    });
    // @ts-expect-error a proxy without a list action has no defaultSort
    void lookupOnly.defaultSort;
  });

  it('rejects when /schema cannot be fetched, and the next call fetches again', async () => {
    http.get.mockRejectedValue(new Error('offline'));
    const api = makeApi([]);
    await expect(api.defaultSort()).rejects.toThrow();
    http.get.mockResolvedValue({ data: schemaWithSort('title:desc') });
    await expect(api.defaultSort()).resolves.toEqual([{ columnName: 'title', direction: 'desc' }]);
  });
});

describe('RestProxyImpl — Idempotency-Key and repeats', () => {
  let http: ReturnType<typeof makeMockAxios>;
  let proxy: RestProxyImpl<number, Item, 'id'>;

  /** The Idempotency-Key header of the n-th call to `fn`. */
  function keyOf(fn: ReturnType<typeof vi.fn>, n = 0): string {
    const config = fn.mock.calls[n][fn === http.get || fn === http.delete ? 1 : 2] as {
      headers: Record<string, string>;
    };
    return config.headers['Idempotency-Key'];
  }

  function signalOf(fn: ReturnType<typeof vi.fn>, n: number): AbortSignal {
    return (fn.mock.calls[n][2] as { signal: AbortSignal }).signal;
  }

  function networkError(code = 'ERR_NETWORK'): AxiosError {
    return new AxiosError('Network Error', code);
  }

  function statusError(status: number, data: unknown = null): AxiosError {
    return new AxiosError(`Request failed with status code ${status}`, 'ERR_BAD_RESPONSE', undefined, undefined, {
      status,
      statusText: '',
      headers: {},
      config: {} as InternalAxiosRequestConfig,
      data,
    });
  }

  beforeEach(() => {
    vi.useFakeTimers();
    http = makeMockAxios();
    proxy = new RestProxyImpl({
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
      validateSchema: false,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('sends a new key on every call, GET included, and exposes it as call.key', async () => {
    http.get.mockResolvedValue({ data: MOCK_ITEM });
    const first = proxy.retrieve(1);
    const second = proxy.retrieve(1);
    await Promise.all([first, second]);
    expect(keyOf(http.get, 0)).toBe(`"${first.key}"`);
    expect(keyOf(http.get, 1)).toBe(`"${second.key}"`);
    expect(first.key).not.toBe(second.key);
  });

  it('sets call.key on the methods that post-process the answer', async () => {
    http.get.mockResolvedValue({
      data: { results: [], offset: 0, limit: 2, count: 0, has_more: false, has_previous: false },
    });
    const call = proxy.listPage();
    expect(call.key).toBeDefined();
    await call;
    expect(keyOf(http.get)).toBe(`"${call.key}"`);
  });

  it('exposes call.key on a route_rest proxy', async () => {
    http.post.mockResolvedValue({ data: MOCK_ITEM });
    const items = route_rest<ViewSetMixin<number, Item, 'id'>>(ItemViewSet, {
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
      validateSchema: false,
    });
    const call = items.create(MOCK_ITEM);
    const key: string = call.key;
    await call;
    expect(keyOf(http.post)).toBe(`"${key}"`);
  });

  it('takes the key from RequestOptions.key, over a header of the same name', async () => {
    class Keyed extends RestProxyImpl<number, Item, 'id'> {
      async again(key: string): Promise<Item> {
        return this.request<Item>('POST', '/again', { key, headers: { 'Idempotency-Key': '"other"' } });
      }
    }
    http.post.mockResolvedValue({ data: MOCK_ITEM });
    const api = new Keyed({
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
      validateSchema: false,
    });
    const call = api.again('first-key') as Promise<Item> & { key?: string };
    expect(call.key).toBe('first-key');
    await call;
    expect(keyOf(http.post)).toBe('"first-key"');
  });

  it('rejects a key an sf-string cannot carry, without sending', async () => {
    class Keyed extends RestProxyImpl<number, Item, 'id'> {
      async bad(): Promise<Item> {
        return this.request<Item>('GET', '', { key: '' });
      }
    }
    const api = new Keyed({
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
      validateSchema: false,
    });
    await expect(api.bad()).rejects.toThrow(TypeError);
    expect(http.get).not.toHaveBeenCalled();
  });

  it('passes attemptTimeoutMs to axios as timeout', async () => {
    http.get.mockResolvedValue({ data: MOCK_ITEM });
    const api = new RestProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
      validateSchema: false,
      attemptTimeoutMs: 5000,
    });
    await api.retrieve(1);
    expect(http.get).toHaveBeenCalledWith('/items/1', expect.objectContaining({ timeout: 5000 }));
  });

  it('repeats a call that got no answer, with the same key, and aborts the repeated attempt', async () => {
    http.post.mockRejectedValueOnce(networkError()).mockResolvedValueOnce({ data: MOCK_ITEM });
    const call = proxy.create(MOCK_ITEM);
    await vi.advanceTimersByTimeAsync(999);
    expect(http.post).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    await expect(call).resolves.toEqual(MOCK_ITEM);
    expect(http.post).toHaveBeenCalledTimes(2);
    expect(keyOf(http.post, 1)).toBe(keyOf(http.post, 0));
    expect(signalOf(http.post, 0).aborted).toBe(true);
    expect(signalOf(http.post, 1).aborted).toBe(false);
  });

  it('reports the call and its repeat to onCallActivity under the call key, background as asked', async () => {
    class Background extends RestProxyImpl<number, Item, 'id'> {
      async quiet(): Promise<Item> {
        return this.request<Item>('POST', '/quiet', { background: true });
      }
    }
    const events: CallEvent[] = [];
    const stop = onCallActivity((event) => events.push(event));
    try {
      http.post.mockRejectedValueOnce(networkError()).mockResolvedValueOnce({ data: MOCK_ITEM });
      const api = new Background({
        basePath: '/items',
        pkFieldName: 'id',
        axiosInstance: http as unknown as typeof axios,
        validateSchema: false,
      });
      const call = api.quiet() as Promise<Item> & { key: string };
      await vi.advanceTimersByTimeAsync(1000);
      await call;
      await vi.advanceTimersByTimeAsync(0);
      const { key } = call;
      expect(events).toEqual([
        { type: 'start', key, background: true },
        { type: 'retry', key, failure: 'lost', attempt: 1, delayMs: 1000 },
        { type: 'settle', key, ok: true },
      ]);
    } finally {
      stop();
    }
  });

  it.each([502, 503, 504])('repeats a call answered %i by a gateway', async (status) => {
    http.post.mockRejectedValueOnce(statusError(status)).mockResolvedValueOnce({ data: MOCK_ITEM });
    const call = proxy.create(MOCK_ITEM);
    await vi.advanceTimersByTimeAsync(1000);
    await expect(call).resolves.toEqual(MOCK_ITEM);
    expect(http.post).toHaveBeenCalledTimes(2);
  });

  it('settles with a gateway status the application answered', async () => {
    const timeout = statusError(504, {
      detail: 'No result for items.create within 3600 s',
      detail_code: 'fastapi-viewsets.result_timeout',
      detail_params: { task: 'items.create', timeout: 3600 },
      field_errors: {},
    });
    http.post.mockRejectedValueOnce(timeout);
    await expect(proxy.create(MOCK_ITEM)).rejects.toBe(timeout);
    expect(http.post).toHaveBeenCalledTimes(1);
  });

  it('repeats a gateway status whose body is not an error body', async () => {
    http.post
      .mockRejectedValueOnce(statusError(502, { detail: 'Bad Gateway' }))
      .mockResolvedValueOnce({ data: MOCK_ITEM });
    const call = proxy.create(MOCK_ITEM);
    await vi.advanceTimersByTimeAsync(1000);
    await expect(call).resolves.toEqual(MOCK_ITEM);
    expect(http.post).toHaveBeenCalledTimes(2);
  });

  it('repeats with the headers the first attempt was sent with, interceptors’ included', async () => {
    const lost = new AxiosError('Network Error', 'ERR_NETWORK', {
      headers: new AxiosHeaders({
        'Idempotency-Key': '"k"',
        'X-Token': 't1',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Content-Length': '12',
      }),
    } as InternalAxiosRequestConfig);
    http.post.mockRejectedValueOnce(lost).mockResolvedValueOnce({ data: MOCK_ITEM });
    class Headed extends RestProxyImpl<number, Item, 'id'> {
      send(): Promise<Item> {
        return this.request<Item>('POST', '', { body: MOCK_ITEM, key: 'k', headers: { 'X-Own': 'o' } });
      }
    }
    const api = new Headed({
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
      validateSchema: false,
    });
    const call = api.send();
    await vi.advanceTimersByTimeAsync(1000);
    await expect(call).resolves.toEqual(MOCK_ITEM);
    expect((http.post.mock.calls[1][2] as { headers: Record<string, string> }).headers).toEqual({
      'X-Token': 't1',
      'X-Own': 'o',
      'Idempotency-Key': '"k"',
    });
  });

  it('repeats at once after the attempt timeout', async () => {
    http.post.mockRejectedValueOnce(networkError('ECONNABORTED')).mockResolvedValueOnce({ data: MOCK_ITEM });
    const call = proxy.create(MOCK_ITEM);
    await vi.advanceTimersByTimeAsync(0);
    await expect(call).resolves.toEqual(MOCK_ITEM);
    expect(http.post).toHaveBeenCalledTimes(2);
  });

  it('settles with any other answer', async () => {
    const notFound = statusError(404);
    http.get.mockRejectedValue(notFound);
    await expect(proxy.retrieve(1)).rejects.toBe(notFound);
    expect(http.get).toHaveBeenCalledTimes(1);
  });

  it('does not repeat an error that is not axios’, nor a cancellation', async () => {
    http.get.mockRejectedValueOnce(new TypeError('boom')).mockRejectedValueOnce(new CanceledError());
    await expect(proxy.retrieve(1)).rejects.toThrow('boom');
    await expect(proxy.retrieve(1)).rejects.toBeInstanceOf(CanceledError);
    expect(http.get).toHaveBeenCalledTimes(2);
  });

  it('rejects with the last error once repeat.withinMs has elapsed', async () => {
    const api = new RestProxyImpl<number, Item, 'id'>({
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
      validateSchema: false,
      repeat: { withinMs: 2500, delayMs: 1000 },
    });
    const errors = [networkError(), networkError(), networkError()];
    http.get.mockRejectedValueOnce(errors[0]).mockRejectedValueOnce(errors[1]).mockRejectedValueOnce(errors[2]);
    const settled = expect(api.retrieve(1)).rejects.toBe(errors[1]);
    await vi.runAllTimersAsync();
    await settled;
    expect(http.get).toHaveBeenCalledTimes(2);
  });
});
