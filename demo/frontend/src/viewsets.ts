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
  constructor() {
    super({ basePath: '/music', pkFieldName: 'id' });
  }

  count = count;
}

export class MusicTrackMuxwsViewSet
  extends MuxwsProxyImpl<number, MusicTrack, 'id'>
  implements BulkViewSetMixin<number, MusicTrack, 'id'>, PaginatedListMixin<MusicTrack>, LookupMixin
{
  constructor(peer: () => Promise<Peer>) {
    super({ basePath: '/music', pkFieldName: 'id', peer });
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

export const restViewSet = new MusicTrackRestViewSet();
export const muxwsViewSet = new MusicTrackMuxwsViewSet(muxwsPeer);

export type MusicTrackViewSet = MusicTrackRestViewSet & MusicTrackMethods;

export function viewSetFor(transport: Transport) {
  return transport === 'rest' ? restViewSet : muxwsViewSet;
}
