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
}

export const musicTrackViewSet = new MusicTrackViewSet();
