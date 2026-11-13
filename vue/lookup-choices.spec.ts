/**
 * Unit tests for lookup-choices.ts. Type-level cases are `@ts-expect-error` lines, checked by
 * `vue-tsc --noEmit` in `npm run lint`.
 */

import axios from 'axios';
import { describe, expect, it, vi } from 'vitest';

import { type FetchLookupChoices, type LookupChoicesParams, lookupChoices } from './lookup-choices';
import { LookupMixin, type LookupItem } from './mixins';
import type { ViewSetProxyBase } from './proxy-base';
import { restViewSet } from './viewset';

interface Item {
  id: number;
  name: string;
}

interface TagLookup {
  id: number;
  name: string;
  color: string;
}

class ItemApi extends restViewSet<Item>()('id', [LookupMixin]) {}
class TagApi extends restViewSet<Item, TagLookup>()('id', [LookupMixin]) {}

/** A ViewSet over an axios instance that records each request URL and answers `data`. */
function viewSet<C extends typeof ItemApi | typeof TagApi>(Api: C, data: unknown[]) {
  const urls: string[] = [];
  const axiosInstance = axios.create({
    adapter: async (config) => {
      urls.push(axios.getUri(config));
      return { data, status: 200, statusText: 'OK', headers: {}, config };
    },
  });
  const api = new Api({ basePath: '/items', validateSchema: false, axiosInstance }) as InstanceType<C>;
  return { api, urls };
}

const ITEMS: LookupItem[] = [
  { group: null, pk: 1, title: 'Apple', icon: 'mdi-food-apple' },
  { group: null, pk: 2, title: 'Banana', icon: null },
];

const TAGS: TagLookup[] = [{ id: 7, name: 'Urgent', color: '#f00' }];

describe('lookupChoices(viewset)', () => {
  it('maps LookupItem pk, title and icon, omitting a null icon', async () => {
    const { api } = viewSet(ItemApi, ITEMS);
    expect(await lookupChoices(api)('a')).toEqual([
      { id: 1, text: 'Apple', icon: 'mdi-food-apple' },
      { id: 2, text: 'Banana' },
    ]);
    expect(await lookupChoices(api)('a').then((choices) => 'icon' in choices[1])).toBe(false);
  });

  it('searches with q', async () => {
    const { api, urls } = viewSet(ItemApi, ITEMS);
    await lookupChoices(api)('ap');
    expect(urls).toEqual(['/items/lookup?q=ap']);
  });

  it('omits q when the search text is empty or missing', async () => {
    const { api, urls } = viewSet(ItemApi, ITEMS);
    const fetchChoices = lookupChoices(api);
    await fetchChoices('');
    await fetchChoices();
    expect(urls).toEqual(['/items/lookup', '/items/lookup']);
  });

  it('resolves one id as ids', async () => {
    const { api, urls } = viewSet(ItemApi, ITEMS);
    await lookupChoices(api)(undefined, 2);
    expect(urls).toEqual(['/items/lookup?ids=2']);
  });

  it('resolves an array of ids as a repeated ids key', async () => {
    const { api, urls } = viewSet(ItemApi, ITEMS);
    await lookupChoices(api)(undefined, [1, 'x']);
    expect(urls).toEqual(['/items/lookup?ids=1&ids=x']);
  });

  it('resolves an empty array of ids to [] without a request', async () => {
    const { api, urls } = viewSet(ItemApi, ITEMS);
    expect(await lookupChoices(api)(undefined, [])).toEqual([]);
    expect(urls).toEqual([]);
  });

  it('searches when idValue is null', async () => {
    const { api, urls } = viewSet(ItemApi, ITEMS);
    await lookupChoices(api)('b', null);
    expect(urls).toEqual(['/items/lookup?q=b']);
  });

  it('calls lookup() as a method of the viewset', async () => {
    const source = {
      items: ITEMS,
      lookup(this: { items: LookupItem[] }) {
        return Promise.resolve(this.items);
      },
    };
    expect(await lookupChoices(source)('')).toHaveLength(2);
  });
});

describe('lookupChoices(viewset, toChoice)', () => {
  it('converts the viewset’s own item type with toChoice', async () => {
    const { api, urls } = viewSet(TagApi, TAGS);
    const fetchChoices = lookupChoices(api, (tag) => ({ id: tag.id, text: tag.name }));
    expect(await fetchChoices(undefined, 7)).toEqual([{ id: 7, text: 'Urgent' }]);
    expect(urls).toEqual(['/items/lookup?ids=7']);
  });
});

describe('lookupChoices(lookup, toChoice)', () => {
  it('passes the params to the given lookup call', async () => {
    const { api, urls } = viewSet(TagApi, TAGS);
    const lookup = vi.fn((params: LookupChoicesParams) => api.lookup({ ...params, activity: 12 }));
    const fetchChoices = lookupChoices(lookup, (tag) => ({ id: tag.id, text: tag.name }));

    expect(await fetchChoices('ur')).toEqual([{ id: 7, text: 'Urgent' }]);
    await fetchChoices(undefined, [7, 8]);
    await fetchChoices(undefined, []);
    expect(lookup.mock.calls).toEqual([[{ q: 'ur' }], [{ ids: [7, 8] }]]);
    expect(urls).toEqual(['/items/lookup?q=ur&activity=12', '/items/lookup?ids=7&ids=8&activity=12']);
  });
});

describe('types', () => {
  /** The shape of a `@dynamicforms/vuetify-inputs` `SelectFetchChoices`. */
  type SelectFetchChoices<T1 = any, T2 = any> = (
    queryValue?: T1,
    idValue?: T2,
  ) => Promise<{ id: any; text: any; icon?: string }[]>;

  function _forms(items: ItemApi, tags: TagApi, proxy: ViewSetProxyBase<number, Item, 'id'>) {
    const fromItems: SelectFetchChoices = lookupChoices(items);
    const fromProxy: SelectFetchChoices = lookupChoices(proxy);
    const fromTags: SelectFetchChoices<string, number | number[]> = lookupChoices(tags, (tag) => ({
      id: tag.id,
      text: tag.color,
    }));
    const fromCall: FetchLookupChoices = lookupChoices(
      (params) => tags.lookup(params),
      (tag) => ({ id: tag.id, text: tag.name }),
    );
    // @ts-expect-error a TagLookup is not a LookupItem: the viewset needs a toChoice
    lookupChoices(tags);
    // @ts-expect-error a TagLookup has no title
    lookupChoices(tags, (tag) => ({ id: tag.id, text: tag.title }));
    // @ts-expect-error a choice has an id and a text
    lookupChoices(items, (item) => ({ text: item.title }));
    return [fromItems, fromProxy, fromTags, fromCall];
  }

  it('checks the three forms', () => {
    expect(_forms).toBeTypeOf('function');
  });
});
