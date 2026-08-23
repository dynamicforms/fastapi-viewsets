/**
 * Unit tests for rest-proxy.ts
 *
 * Tests cover:
 * 1. How to declare a ViewSet class and pass it to route_rest
 * 2. How to instantiate a REST proxy with route_rest
 * 3. That the proxy calls the correct HTTP methods on the correct URLs
 */

import axios from 'axios';
import { beforeEach, describe, expect, it, vi } from 'vitest';

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
    expect(http.get).toHaveBeenCalledWith('/items');
    expect(result).toEqual(MOCK_ITEMS);
  });

  it('retrieve() — GET /items/1', async () => {
    http.get.mockResolvedValue({ data: MOCK_ITEM });
    const result = await proxy.retrieve(1);
    expect(http.get).toHaveBeenCalledWith('/items/1');
    expect(result).toEqual(MOCK_ITEM);
  });

  it('create() — POST /items', async () => {
    http.post.mockResolvedValue({ data: MOCK_ITEM });
    const result = await proxy.create(MOCK_ITEM);
    expect(http.post).toHaveBeenCalledWith('/items', MOCK_ITEM);
    expect(result).toEqual(MOCK_ITEM);
  });

  it('update() — PUT /items/1', async () => {
    const updated = { ...MOCK_ITEM, name: 'Updated' };
    http.put.mockResolvedValue({ data: updated });
    const result = await proxy.update(1, updated);
    expect(http.put).toHaveBeenCalledWith('/items/1', updated);
    expect(result).toEqual(updated);
  });

  it('partialUpdate() — PATCH /items/1', async () => {
    const patch = { name: 'Patched' };
    http.patch.mockResolvedValue({ data: { ...MOCK_ITEM, ...patch } });
    const result = await proxy.partialUpdate(1, patch);
    expect(http.patch).toHaveBeenCalledWith('/items/1', patch);
    expect(result).toEqual({ ...MOCK_ITEM, ...patch });
  });

  it('destroy() — DELETE /items/1', async () => {
    http.delete.mockResolvedValue({ data: { deleted: 1 } });
    const result = await proxy.destroy(1);
    expect(http.delete).toHaveBeenCalledWith('/items/1');
    expect(result).toEqual({ deleted: 1 });
  });

  it('bulkCreate() — POST /items/bulk', async () => {
    http.post.mockResolvedValue({ data: MOCK_ITEMS });
    const result = await proxy.bulkCreate(MOCK_ITEMS);
    expect(http.post).toHaveBeenCalledWith('/items/bulk', MOCK_ITEMS);
    expect(result).toEqual(MOCK_ITEMS);
  });

  it('bulkUpdate() — PUT /items/bulk', async () => {
    const records = { 1: MOCK_ITEM };
    http.put.mockResolvedValue({ data: MOCK_ITEMS });
    const result = await proxy.bulkUpdate(records);
    expect(http.put).toHaveBeenCalledWith('/items/bulk', records);
    expect(result).toEqual(MOCK_ITEMS);
  });

  it('bulkPartialUpdate() — PATCH /items/bulk', async () => {
    const records = { 1: { name: 'Patched' } };
    http.patch.mockResolvedValue({ data: MOCK_ITEMS });
    const result = await proxy.bulkPartialUpdate(records);
    expect(http.patch).toHaveBeenCalledWith('/items/bulk', records);
    expect(result).toEqual(MOCK_ITEMS);
  });

  it('bulkDestroy() — DELETE /items/bulk', async () => {
    http.delete.mockResolvedValue({ data: [{ deleted: 1 }] });
    const result = await proxy.bulkDestroy([1, 2]);
    expect(http.delete).toHaveBeenCalledWith('/items/bulk', { data: [1, 2] });
    expect(result).toEqual([{ deleted: 1 }]);
  });

  it('lookup() — GET /items/lookup', async () => {
    const lookupItems = [{ group: null, pk: 1, title: 'Test item', icon: null }];
    http.get.mockResolvedValue({ data: lookupItems });
    const result = await proxy.lookup();
    expect(http.get).toHaveBeenCalledWith('/items/lookup');
    expect(result).toEqual(lookupItems);
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
    expect(http.delete).toHaveBeenCalledWith('/items/1');
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
    expect(http.delete).toHaveBeenCalledWith('/items/1');
  });

  it('bulkDestroy()', async () => {
    http.delete.mockResolvedValue({ data: [] });
    await proxy.bulkDestroy([1, 2]);
    expect(http.delete).toHaveBeenCalledWith('/items/bulk', { data: [1, 2] });
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
    expect(http.get).toHaveBeenCalledWith('/items');
    expect(items).toEqual([MOCK_ITEM]);
  });

  it('retrieve() still works on extended class', async () => {
    http.get.mockResolvedValue({ data: MOCK_ITEM });
    const item = await api.retrieve(1);
    expect(http.get).toHaveBeenCalledWith('/items/1');
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
    // Regression: the check lives on the shared base class, but it reads `this.http`, which the
    // subclass constructor assigns after super() returns. Kicking it off from the base
    // constructor made every call throw on an undefined axios instance — and since the check
    // swallows its own errors, it went silently dead instead of failing.
    mockSchema(FULL_SCHEMA);
    new (viewSetDeclaring([BulkViewSetMixin, LookupMixin]))({
      basePath: '/items',
      pkFieldName: 'id',
      axiosInstance: http as unknown as typeof axios,
    });
    await vi.waitFor(() => expect(http.get).toHaveBeenCalledWith('/items/schema'));
  });

  it('no warnings when BE matches what the ViewSet declared', async () => {
    await createAndValidate(FULL_SCHEMA);
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('a ViewSet declaring one mixin says nothing about actions it never claimed', async () => {
    // The reported bug: a BE viewset built from a single mixin (or none) made the FE warn about
    // every standard action, because the check asked which methods exist on the proxy — and they
    // all do, unconditionally — instead of asking what this ViewSet declared.
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
    expect(http.get).not.toHaveBeenCalledWith('/items/schema');
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
    // Here `typeof this[name]` IS honest: the base implements no custom actions, so whatever
    // answers is something this ViewSet's own author wrote.
    await createAndValidate(
      { paths: { ...FULL_SCHEMA.paths, '/items/export': { get: {} } } },
      [BulkViewSetMixin, LookupMixin],
      { export: () => Promise.resolve(null) },
    );
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('warns about a custom BE endpoint the ViewSet has no method for', async () => {
    await createAndValidate({ paths: { ...FULL_SCHEMA.paths, '/items/import': { post: {} } } });
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
