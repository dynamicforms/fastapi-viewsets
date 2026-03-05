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

import { BulkViewSetMixin, ListMixin, LookupMixin, ReadOnlyViewSetMixin, RetrieveMixin, ViewSetMixin } from './mixins';
import { RestProxyImpl, route_rest } from './rest-proxy';

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
