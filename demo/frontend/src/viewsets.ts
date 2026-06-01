import { BulkViewSetMixin, LookupMixin } from '../../../vue/mixins';
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

export class MusicTrackViewSet extends RestProxyImpl<number, MusicTrack, 'id'>
  implements BulkViewSetMixin<number, MusicTrack, 'id'>, LookupMixin
{
  constructor() {
    super({ basePath: '/music', pkFieldName: 'id' });
  }

  // The method name must match the BE endpoint path segment (GET /music/count → count).
  // Renaming it (e.g. to getCount) will trigger a schema mismatch warning in the console on startup.
  async count(): Promise<number> {
    const res = await this.http.get<number>(`${this.basePath}/count`);
    return res.data;
  }
}

export const musicTrackViewSet = new MusicTrackViewSet();
