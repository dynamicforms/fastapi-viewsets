import { connect, type Peer } from 'muxws';

import { BulkViewSetMixin, LookupMixin, PaginatedListMixin } from '../../../vue/mixins';
import { MuxwsProxyImpl } from '../../../vue/muxws-proxy';
import type { HttpMethod, RequestOptions } from '../../../vue/proxy-base';
import { RestProxyImpl } from '../../../vue/rest-proxy';

export interface MusicTrack {
  id: number;
  title: string;
  artist: string;
  year: number;
  duration: string;
  genres: string[];
  rating: number;
  favorite: boolean;
  play_count: number;
  moods: string[];
  language: string;
}

export type Transport = 'rest' | 'muxws';

/**
 * Which backend serves the data. Same API, same records, same page numbers - one holds them in a
 * dict, the other in SQLite through the Django ORM, which can push the filter, the ordering and
 * the slice into SQL. Compare them at a high offset: the in-memory one walks to the page, the
 * database one seeks to it.
 */
export type Backend = 'memory' | 'db';

const BASE_PATHS: Record<Backend, string> = { memory: '/music', db: '/music-db' };

/**
 * The ViewSet's own methods, written once against `request()` so that the same body works on both
 * transports. Only the base class differs between the two proxies below — everything a ViewSet
 * actually declares is shared here.
 */
interface MusicTrackMethods {
  count(): Promise<number>;
}

/** Custom endpoint: the method name must match the BE path segment (GET /music/count → count). */
async function count(this: { request<R>(m: HttpMethod, p: string, o?: RequestOptions): Promise<R> }): Promise<number> {
  return this.request<number>('GET', '/count');
}

export class MusicTrackRestViewSet
  extends RestProxyImpl<number, MusicTrack, 'id'>
  implements BulkViewSetMixin<number, MusicTrack, 'id'>, PaginatedListMixin<MusicTrack>, LookupMixin
{
  constructor(basePath: string) {
    super({ basePath, pkFieldName: 'id' });
  }

  count = count;
}

export class MusicTrackMuxwsViewSet
  extends MuxwsProxyImpl<number, MusicTrack, 'id'>
  implements BulkViewSetMixin<number, MusicTrack, 'id'>, PaginatedListMixin<MusicTrack>, LookupMixin
{
  constructor(basePath: string, peer: () => Promise<Peer>) {
    super({ basePath, pkFieldName: 'id', peer });
  }

  count = count;
}

/**
 * One peer for the whole application, connected on first use.
 *
 * muxws keeps a Peer alive across its own reconnects, so this is resolved once and never
 * re-resolved — the object survives even though the socket underneath it may not.
 */
let peerPromise: Promise<Peer> | undefined;

export function muxwsPeer(): Promise<Peer> {
  if (!peerPromise) {
    const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
    peerPromise = connect(url);
  }
  return peerPromise;
}

export type MusicTrackViewSet = MusicTrackRestViewSet & MusicTrackMethods;

/** One proxy per (transport, backend) pair, built once - each does a schema check on construction. */
const proxies = new Map<string, MusicTrackRestViewSet | MusicTrackMuxwsViewSet>();

export function viewSetFor(transport: Transport, backend: Backend = 'memory') {
  const key = `${transport}:${backend}`;
  let proxy = proxies.get(key);
  if (!proxy) {
    proxy =
      transport === 'rest'
        ? new MusicTrackRestViewSet(BASE_PATHS[backend])
        : new MusicTrackMuxwsViewSet(BASE_PATHS[backend], muxwsPeer);
    proxies.set(key, proxy);
  }
  return proxy;
}
