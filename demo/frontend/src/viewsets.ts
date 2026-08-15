import { connect, type Peer } from 'muxws';

import {
  BulkViewSetMixin,
  CursorListMixin,
  LookupMixin,
  PaginatedListMixin,
  RetrieveMixin,
} from '../../../vue/mixins';
import { ViewSetInternals } from '../../../vue/proxy-base';
import { muxwsViewSet, restViewSet } from '../../../vue/viewset';

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

/**
 * The custom endpoint, written once and mixed into all four ViewSets.
 *
 * A class-expression mixin rather than a plain method, because the four classes have no common
 * ancestor to put it on: each comes from its own `restViewSet`/`muxwsViewSet` call. The body is
 * written against `request()`, which is why the same one serves both transports.
 *
 * The return type is annotated on purpose: an inferred one names the anonymous class and leaks its
 * protected members into the emitted .d.ts (TS4094).
 */
function WithCount<TBase extends abstract new (...args: any[]) => ViewSetInternals>(
  Base: TBase,
): TBase & (abstract new (...args: any[]) => MusicTrackMethods) {
  abstract class Counting extends Base {
    /** The method name must match the BE path segment (GET /music/count → count). */
    async count(): Promise<number> {
      return this.request<number>('GET', '/count');
    }
  }
  return Counting;
}

/** What the in-memory BE viewset serves. */
const MEMORY = [BulkViewSetMixin, CursorListMixin, PaginatedListMixin, LookupMixin];
/** What the Django-backed one serves - it was built from two mixins, not six. */
const DB = [CursorListMixin, RetrieveMixin];

export class MusicTrackRestViewSet extends WithCount(restViewSet<MusicTrack>()('id', MEMORY)) {}
export class MusicTrackDbRestViewSet extends WithCount(restViewSet<MusicTrack>()('id', DB)) {}
export class MusicTrackMuxwsViewSet extends WithCount(muxwsViewSet<MusicTrack>()('id', MEMORY)) {}
export class MusicTrackDbMuxwsViewSet extends WithCount(muxwsViewSet<MusicTrack>()('id', DB)) {}

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

/**
 * Any of the four. A union rather than an intersection: the db ViewSets are no longer subtypes of
 * the memory ones, so what callers may rely on is what all four have.
 */
export type MusicTrackViewSet =
  | MusicTrackRestViewSet
  | MusicTrackDbRestViewSet
  | MusicTrackMuxwsViewSet
  | MusicTrackDbMuxwsViewSet;

/** One proxy per (transport, backend) pair, built once - each does a schema check on construction. */
const proxies = new Map<string, MusicTrackViewSet>();

export function viewSetFor(transport: Transport, backend: Backend = 'memory') {
  const key = `${transport}:${backend}`;
  let proxy = proxies.get(key);
  if (!proxy) {
    proxy =
      transport === 'rest'
        ? new (backend === 'db' ? MusicTrackDbRestViewSet : MusicTrackRestViewSet)({
            basePath: BASE_PATHS[backend],
          })
        : new (backend === 'db' ? MusicTrackDbMuxwsViewSet : MusicTrackMuxwsViewSet)({
            basePath: BASE_PATHS[backend],
            peer: muxwsPeer,
          });
    proxies.set(key, proxy);
  }
  return proxy;
}
