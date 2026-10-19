/**
 * Unit tests for viewset.ts — the ViewSet class factory.
 *
 * What the factory adds over `route_rest` is that the object is the consumer's own class: its own
 * methods exist at runtime, and its declaration reaches the schema check. Both halves are covered
 * here. The type-level half — that an undeclared action does not compile — cannot be asserted by
 * vitest, which type-checks nothing; those are `@ts-expect-error` lines, which `vue-tsc --noEmit`
 * in `npm run lint` fails on if they ever stop being errors.
 */

import axios from 'axios';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { muxwsViewSet, restViewSet, type ViewSetMixinClass } from './viewset';

import {
  BulkViewSetMixin,
  CursorListMixin,
  LookupMixin,
  ReadOnlyViewSetMixin,
  RetrieveMixin,
  ViewSetInternals,
} from './index';

interface Item {
  id: number;
  name: string;
}

const MOCK_ITEMS: Item[] = [
  { id: 1, name: 'Test item' },
  { id: 2, name: 'Second item' },
];

/** Exactly what a ReadOnly + Lookup BE viewset serves. */
const READONLY_LOOKUP_SCHEMA = {
  paths: {
    '/items': { get: {} },
    '/items/{pk}': { get: {} },
    '/items/lookup': { get: {} },
    '/items/schema': { get: {} },
  },
};

/** The config every REST attempt carries: the call's own, plus the key, the timeout and the abort signal. */
function sent(config: { params?: unknown; headers?: Record<string, string>; data?: unknown } = {}) {
  return {
    ...config,
    headers: { ...config.headers, 'Idempotency-Key': expect.stringMatching(/^"[0-9a-f-]{36}"$/) },
    timeout: 30000,
    signal: expect.any(AbortSignal),
  };
}

function makeMockAxios() {
  return {
    get: vi.fn().mockResolvedValue({ data: MOCK_ITEMS }),
    post: vi.fn().mockResolvedValue({ data: MOCK_ITEMS[0] }),
    put: vi.fn().mockResolvedValue({ data: MOCK_ITEMS[0] }),
    patch: vi.fn().mockResolvedValue({ data: MOCK_ITEMS[0] }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  };
}

describe('restViewSet', () => {
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
      url === '/items/schema' ? Promise.resolve({ data: schema }) : Promise.resolve({ data: MOCK_ITEMS }),
    );
  }

  class ItemApi extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin, LookupMixin]) {
    /** A custom endpoint, written against request() so the same body works on either transport. */
    async count(): Promise<number> {
      return this.request<number>('GET', '/count');
    }
  }

  function build(schema: object = READONLY_LOOKUP_SCHEMA) {
    mockSchema(schema);
    return new ItemApi({ basePath: '/items', axiosInstance: http as unknown as typeof axios });
  }

  it('produces a working proxy for the actions it declares', async () => {
    const api = build();
    await expect(api.list()).resolves.toEqual(MOCK_ITEMS);
    expect(http.get).toHaveBeenCalledWith('/items', sent({ headers: { 'X-List-Shape': 'plain' } }));
  });

  it('binds pkFieldName from the factory rather than the constructor', async () => {
    const api = build();
    await api.retrieve(2);
    expect(http.get).toHaveBeenCalledWith('/items/2', sent());
    // No cast: pkFieldName is part of a factory-built class's own type surface (typed as the
    // literal 'id', not widened to string), not just present at runtime - a generic caller (a grid
    // or table component) holding the instance can read which field is the pk without one.
    expect(api.pkFieldName).toBe('id');
  });

  it('carries the declaration to the schema check, which then finds nothing to report', async () => {
    build();
    await vi.waitFor(() => expect(http.get).toHaveBeenCalledWith('/items/schema', sent()));
    await new Promise((r) => setTimeout(r, 0));
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('warns when the BE serves an action the ViewSet did not declare', async () => {
    build({ paths: { ...READONLY_LOOKUP_SCHEMA.paths, '/items/bulk': { post: {} } } });
    await vi.waitFor(() => expect(warnSpy).toHaveBeenCalled());
    expect(warnSpy.mock.calls[0][0] as string).toContain("BE serves 'POST /items/bulk'");
  });

  it('puts a custom endpoint on the prototype, so it is not reported as missing', async () => {
    // route_rest cannot do this: it returns a bare proxy, so a custom method typed into M is
    // undefined when called. Here the object is ItemApi itself.
    const api = build({
      paths: { ...READONLY_LOOKUP_SCHEMA.paths, '/items/count': { get: { 'x-viewset-action': 'count' } } },
    });
    expect(typeof Object.getPrototypeOf(api).count).toBe('function');
    await vi.waitFor(() => expect(http.get).toHaveBeenCalledWith('/items/schema', sent()));
    await new Promise((r) => setTimeout(r, 0));
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('reports a custom BE endpoint the ViewSet has no method for', async () => {
    class NoCount extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin, LookupMixin]) {}
    mockSchema({
      paths: { ...READONLY_LOOKUP_SCHEMA.paths, '/items/count': { get: { 'x-viewset-action': 'count' } } },
    });
    new NoCount({ basePath: '/items', axiosInstance: http as unknown as typeof axios });
    await vi.waitFor(() => expect(warnSpy).toHaveBeenCalled());
    expect(warnSpy.mock.calls[0][0] as string).toContain("no 'count()' method");
  });

  it('narrows by calling the factory again, not by subclassing', async () => {
    // A factory-built class pins `declares` to the exact tuple it was given, so restating it in a
    // subclass is TS2417 - see the @ts-expect-error in 'the type surface' below. The smaller
    // ViewSet is its own class, which also keeps its type honest about what it can do.
    class ItemDbApi extends restViewSet<Item>()('id', [CursorListMixin]) {}
    mockSchema({ paths: { '/items': { get: {} }, '/items/schema': { get: {} } } });
    new ItemDbApi({ basePath: '/items', axiosInstance: http as unknown as typeof axios });
    await vi.waitFor(() => expect(http.get).toHaveBeenCalledWith('/items/schema', sent()));
    await new Promise((r) => setTimeout(r, 0));
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('assigns nothing after super(), so the base constructor is not clobbered', () => {
    // Regression guard: the factory injects pkFieldName into the argument expression rather than
    // assigning it afterwards. Under useDefineForClassFields an assignment would emit a [[Define]]
    // running after super() and would overwrite what the base had set.
    const api = build();
    expect(Object.keys(api).sort()).toEqual(
      [
        'attemptTimeoutMs',
        'basePath',
        'declaredMixins',
        'eventClientOption',
        'http',
        'pkFieldName',
        'repeatOptions',
        'schemaValidationEnabled',
        'topicOption',
      ].sort(),
    );
  });

  it('normalises a trailing slash on basePath through the factory constructor', async () => {
    mockSchema(READONLY_LOOKUP_SCHEMA);
    const api = new ItemApi({ basePath: '/items/', axiosInstance: http as unknown as typeof axios });
    await api.list();
    expect(http.get).toHaveBeenCalledWith('/items', sent({ headers: { 'X-List-Shape': 'plain' } }));
  });

  it('still honours validateSchema: false per instance', async () => {
    mockSchema(READONLY_LOOKUP_SCHEMA);
    new ItemApi({ basePath: '/items', validateSchema: false, axiosInstance: http as unknown as typeof axios });
    await new Promise((r) => setTimeout(r, 0));
    expect(http.get.mock.calls.map(([url]) => url)).not.toContain('/items/schema');
  });

  it('descends from ViewSetInternals, which is what makes request() reachable', () => {
    expect(build()).toBeInstanceOf(ViewSetInternals);
  });
});

describe('muxwsViewSet', () => {
  it('builds a muxws-backed ViewSet with the same declaration', async () => {
    const sent: unknown[] = [];
    const peer = {
      open: (payload: unknown) => {
        sent.push(payload);
        return {
          replyHeadersArrived: Promise.resolve(),
          replyHeaders: { ':status': '200' },
          result: () => Promise.resolve(MOCK_ITEMS),
          close: () => {},
        };
      },
    };
    class ItemWs extends muxwsViewSet<Item>()('id', [CursorListMixin, RetrieveMixin]) {}
    const api = new ItemWs({
      basePath: '/items',
      validateSchema: false,
      peer: () => Promise.resolve(peer as never),
    });
    expect(api).toBeInstanceOf(ViewSetInternals);
    expect((ItemWs as unknown as { declares: unknown[] }).declares).toHaveLength(2);
  });
});

describe('the type surface', () => {
  class ItemApi extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin, LookupMixin]) {
    async count(): Promise<number> {
      return this.request<number>('GET', '/count');
    }
  }

  // Never called: these bodies are assertions for vue-tsc, and dereferencing the fake instance at
  // runtime would only throw.
  function _surface(api: ItemApi) {
    void api.list;
    void api.retrieve;
    void api.lookup;
    void api.count;

    // @ts-expect-error create was not declared
    void api.create;
    // @ts-expect-error listCursor was not declared
    void api.listCursor;
    // @ts-expect-error basePath is protected
    void api.basePath;
  }

  function _pkType(api: ItemApi) {
    void api.retrieve(1);
    // @ts-expect-error Item['id'] is a number
    void api.retrieve('1');
  }

  it('exposes the declared actions and nothing else, and types the pk from the model', () => {
    expect([_surface, _pkType]).toHaveLength(2);
  });

  it('lets a declared action be overridden with a method', () => {
    // The reason the surface is an intersection rather than Pick<> of a table: a mapped type
    // re-emits a method as a property, and TS2425 then forbids this class.
    class Cached extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin]) {
      override async list(): Promise<Item[]> {
        return super.list();
      }
    }
    expect(typeof Cached.prototype.list).toBe('function');
  });

  it('rejects a pk field that is not a field of the model', () => {
    // @ts-expect-error 'nope' is not a field of Item
    void restViewSet<Item>()('nope', [BulkViewSetMixin]);
    expect(true).toBe(true);
  });

  it('refuses a restated declaration on a subclass of a factory-built class', () => {
    class Base extends restViewSet<Item>()('id', [ReadOnlyViewSetMixin]) {}
    // @ts-expect-error TS2417 on the class itself: declares is pinned to the tuple the factory got
    class Narrowed extends Base {
      static declares = [CursorListMixin];
    }
    expect(Narrowed).toBeDefined();
  });
});

describe('a ViewSet with custom endpoints only', () => {
  interface Report {
    id: number;
    name: string;
    approved: boolean;
  }

  const REPORT: Report = { id: 7, name: 'report 7', approved: true };

  /** What `route_viewset` serves for a BE viewset with three custom endpoints and no mixin. */
  const CUSTOM_ONLY_SCHEMA = {
    paths: {
      '/reports/schema': { get: {} },
      '/reports/summary': { get: { 'x-viewset-action': 'summary' } },
      '/reports/top-rated': { get: { 'x-viewset-action': 'top_rated' } },
      '/reports/{pk}/approve': { post: { 'x-viewset-action': 'approve' } },
    },
  };

  class ReportApi extends restViewSet<Report>()('id', []) {
    async summary(): Promise<Report> {
      return this.request<Report>('GET', '/summary');
    }

    async topRated(): Promise<Report[]> {
      return this.request<Report[]>('GET', '/top-rated');
    }

    async approve(pk: number): Promise<Report> {
      return this.request<Report>('POST', `/${pk}/approve`);
    }
  }

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
      url === '/reports/schema' ? Promise.resolve({ data: schema }) : Promise.resolve({ data: REPORT }),
    );
    http.post.mockResolvedValue({ data: REPORT });
  }

  async function schemaChecked() {
    await vi.waitFor(() => expect(http.get).toHaveBeenCalledWith('/reports/schema', sent()));
    await new Promise((r) => setTimeout(r, 0));
  }

  function build<C extends new (options: { basePath: string; axiosInstance: typeof axios }) => object>(
    ViewSet: C,
    schema: object = CUSTOM_ONLY_SCHEMA,
  ): InstanceType<C> {
    mockSchema(schema);
    return new ViewSet({ basePath: '/reports', axiosInstance: http as unknown as typeof axios }) as InstanceType<C>;
  }

  it('calls its custom endpoints', async () => {
    const api = build(ReportApi);
    await expect(api.summary()).resolves.toEqual(REPORT);
    expect(http.get).toHaveBeenCalledWith('/reports/summary', sent());
    await api.topRated();
    expect(http.get).toHaveBeenCalledWith('/reports/top-rated', sent());
    await expect(api.approve(7)).resolves.toEqual(REPORT);
    expect(http.post).toHaveBeenCalledWith('/reports/7/approve', undefined, sent());
  });

  it('agrees with a BE that serves exactly its custom endpoints', async () => {
    build(ReportApi);
    await schemaChecked();
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('matches an endpoint under a path parameter by the BE method name, not by the path', async () => {
    // `POST /reports/{pk}/approve` is served by `approve()`; the first path segment is `{pk}`
    class ApproveOnly extends restViewSet<Report>()('id', []) {
      async approve(pk: number): Promise<Report> {
        return this.request<Report>('POST', `/${pk}/approve`);
      }
    }
    build(ApproveOnly, { paths: { '/reports/{pk}/approve': CUSTOM_ONLY_SCHEMA.paths['/reports/{pk}/approve'] } });
    await schemaChecked();
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('matches a snake_case BE method by its camelCase name or by the name itself', async () => {
    // `GET /reports/top-rated` is served by `top_rated` on the BE: `topRated()` and `top_rated()` both answer
    class SnakeCase extends restViewSet<Report>()('id', []) {
      async top_rated(): Promise<Report[]> {
        return this.request<Report[]>('GET', '/top-rated');
      }
    }
    build(SnakeCase, { paths: { '/reports/top-rated': CUSTOM_ONLY_SCHEMA.paths['/reports/top-rated'] } });
    await schemaChecked();
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('reports a custom BE endpoint the ViewSet has no method for, by both names it would accept', async () => {
    class NoTopRated extends restViewSet<Report>()('id', []) {
      async summary(): Promise<Report> {
        return this.request<Report>('GET', '/summary');
      }

      async approve(pk: number): Promise<Report> {
        return this.request<Report>('POST', `/${pk}/approve`);
      }
    }
    build(NoTopRated);
    await vi.waitFor(() => expect(warnSpy).toHaveBeenCalledTimes(1));
    const message = warnSpy.mock.calls[0][0] as string;
    expect(message).toContain(
      "BE serves 'GET /reports/top-rated' but the ViewSet has no 'topRated()' or 'top_rated()' method",
    );
    expect(message).not.toContain('summary');
    expect(message).not.toContain('approve');
  });

  it('does not check an operation the BE names no method for', async () => {
    // a BE without `x-viewset-action` on its operations: the path does not say which method serves it
    build(ReportApi, { paths: { '/reports/{pk}/history': { get: {} }, '/reports/export-all': { get: {} } } });
    await schemaChecked();
    expect(warnSpy).not.toHaveBeenCalled();
  });

  it('reports a standard endpoint the BE serves, since the ViewSet declared none', async () => {
    build(ReportApi, { paths: { ...CUSTOM_ONLY_SCHEMA.paths, '/reports': { get: {} } } });
    await vi.waitFor(() => expect(warnSpy).toHaveBeenCalledTimes(1));
    expect(warnSpy.mock.calls[0][0] as string).toContain("BE serves 'GET /reports' but the ViewSet declares no list");
  });

  it('builds a muxws ViewSet with custom endpoints only', async () => {
    const opened: unknown[] = [];
    const schemaRead = vi.fn(() => Promise.resolve(CUSTOM_ONLY_SCHEMA));
    const peer = {
      open: (payload: unknown) => {
        opened.push(payload);
        return {
          replyHeadersArrived: Promise.resolve(),
          replyHeaders: { ':status': '200' },
          result: schemaRead,
          close: () => {},
        };
      },
    };
    class ReportWs extends muxwsViewSet<Report>()('id', []) {
      async summary(): Promise<Report> {
        return this.request<Report>('GET', '/summary');
      }

      async topRated(): Promise<Report[]> {
        return this.request<Report[]>('GET', '/top-rated');
      }

      async approve(pk: number): Promise<Report> {
        return this.request<Report>('POST', `/${pk}/approve`);
      }
    }
    new ReportWs({ basePath: '/reports', peer: () => Promise.resolve(peer as never) });
    await vi.waitFor(() => expect(schemaRead).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 10));
    expect(opened).toHaveLength(1);
    expect(warnSpy).not.toHaveBeenCalled();
  });

  // Never called: these bodies are assertions for vue-tsc.
  function _surface(api: ReportApi) {
    void api.summary;
    void api.topRated;
    void api.approve;
    void api.pkFieldName;
    // @ts-expect-error a ViewSet with custom endpoints only has no standard action
    void api.list;
    // @ts-expect-error a ViewSet with custom endpoints only has no standard action
    void api.retrieve;
  }

  it('exposes its own methods and no standard action', () => {
    expect(_surface).toBeTypeOf('function');
  });

  it('still refuses a list annotated as ViewSetMixinClass[], which hides what it holds', () => {
    const declares: ViewSetMixinClass[] = [RetrieveMixin];
    // @ts-expect-error the annotation erases the mixins: the factory cannot tell which actions there are
    class Hidden extends restViewSet<Report>()('id', declares) {}
    expect(Hidden).toBeDefined();
  });
});
