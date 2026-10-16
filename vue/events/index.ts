/**
 * The frontend side of the events API: the event client, `watch()` support for ViewSets and the
 * polling ladder the client schedules its polls with.
 */

export type {
  EventAxiosLike,
  EventClient,
  EventClientOptions,
  EventDefinition,
  EventHandlers,
  EventMeta,
  EventPeerLike,
  EventStreamHandler,
  EventStreamLike,
  EventSubscription,
} from './client';
export { attachEvents, createEventClient, defineEvent, eventsOf, handleEventStream } from './client';

export type { Change, KeyOf, RecordOf, RecordsChanged, ViewSetWatch, WatchOptions } from './changes';
export { applyChanges, RECORDS_CHANGED } from './changes';

export type { Draw, PollLadderState, PollSettings } from './poll';
export { DEFAULT_POLL_SETTINGS, jitterMultiplier, ladderInterval, nextDelay, PollLadder } from './poll';
