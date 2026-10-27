import axios from 'axios';
import { connect, type Peer } from 'muxws';

import { attachEvents, createEventClient } from '../../../vue/events';
import {
  BulkViewSetMixin,
  CursorListMixin,
  LookupMixin,
  PaginatedListMixin,
  RetrieveMixin,
  UpdateMixin,
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
 * Which backend serves the data: a dict, or SQLite through the Django ORM, which pushes filter,
 * ordering and slice into SQL. Same API and records.
 */
export type Backend = 'memory' | 'db';

const BASE_PATHS: Record<Backend, string> = { memory: '/music', db: '/music-db' };

/** The ViewSet's own methods, shared by the four proxies below. */
interface MusicTrackMethods {
  count(): Promise<number>;
}

/**
 * The custom endpoint as a mixin: the four classes have no common ancestor. `request()` serves both
 * transports. The return type is annotated: an inferred one leaks protected members into the .d.ts
 * (TS4094).
 */
function WithCount<TBase extends abstract new (...args: any[]) => ViewSetInternals>(
  Base: TBase,
): TBase & (abstract new (...args: any[]) => MusicTrackMethods) {
  abstract class Counting extends Base {
    /** Served by the BE method `count` (GET /music/count). */
    async count(): Promise<number> {
      return this.request<number>('GET', '/count');
    }
  }
  return Counting;
}

/** What the in-memory BE viewset serves. */
const MEMORY = [BulkViewSetMixin, CursorListMixin, PaginatedListMixin, LookupMixin];
/** What the Django-backed one serves: cursor list, retrieve, update and partial update. */
const DB = [CursorListMixin, RetrieveMixin, UpdateMixin];

export class MusicTrackRestViewSet extends WithCount(restViewSet<MusicTrack>()('id', MEMORY)) {}
export class MusicTrackDbRestViewSet extends WithCount(restViewSet<MusicTrack>()('id', DB)) {}
export class MusicTrackMuxwsViewSet extends WithCount(muxwsViewSet<MusicTrack>()('id', MEMORY)) {}
export class MusicTrackDbMuxwsViewSet extends WithCount(muxwsViewSet<MusicTrack>()('id', DB)) {}

/** Where the backend's `register_events` serves `subscribe`, `unsubscribe` and `poll`. */
const EVENTS_PATH = '/events';

/** The REST ViewSets' event client: no peer, so it polls over axios on the polling ladder. */
const restEvents = createEventClient({ basePath: EVENTS_PATH, axios });

/**
 * One peer for the application, connected on first use; muxws keeps it alive across reconnects. Its
 * event client takes the peer's `onStream` slot and is found by the muxws ViewSets with `eventsOf(peer)`.
 */
let peerPromise: Promise<Peer> | undefined;

export function muxwsPeer(): Promise<Peer> {
  if (!peerPromise) {
    const url = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
    peerPromise = connect(url).then((peer) => {
      attachEvents(peer);
      createEventClient({ basePath: EVENTS_PATH, peer, axios });
      return peer;
    });
  }
  return peerPromise;
}

/** Any of the four; callers may rely only on what all four have. */
export type MusicTrackViewSet =
  MusicTrackRestViewSet | MusicTrackDbRestViewSet | MusicTrackMuxwsViewSet | MusicTrackDbMuxwsViewSet;

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
            events: restEvents,
          })
        : new (backend === 'db' ? MusicTrackDbMuxwsViewSet : MusicTrackMuxwsViewSet)({
            basePath: BASE_PATHS[backend],
            peer: muxwsPeer,
          });
    proxies.set(key, proxy);
  }
  return proxy;
}
