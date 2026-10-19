# Events (Vue) — API Reference

```ts
import {
  attachEvents, createEventClient, DEFAULT_CHECK_INTERVAL_MS, defineEvent, eventsOf, handleEventStream,
  applyChanges, RECORDS_CHANGED,
  DEFAULT_POLL_SETTINGS, jitterMultiplier, ladderInterval, nextDelay, PollLadder,
} from '@dynamicforms/fastapi-viewsets';
```

The frontend side of the events API: an event client that follows topics over a muxws peer, over
axios, or both, the polling ladder it schedules its polls with, and the `records_changed` payload
that [`watch()`](./vue-mixins#watch) delivers. The guide is [Events (Vue)](../guide/vue-events); the
backend side is [`fastapi_viewsets.events`](./events).

## createEventClient

```ts
function createEventClient(options: EventClientOptions): EventClient;

interface EventClientOptions {
  /** The path the BE's `register_events` serves, e.g. `/events`. */
  basePath: string;
  /** Carries pushes and, while open, the client's calls. */
  peer?: EventPeerLike;
  /** Carries the client's polls while there is no open peer. */
  axios?: EventAxiosLike;
  /** The ladder's constants. */
  pollSettings?: PollSettings;
  /** The ladder's jitter draw. */
  draw?: Draw;
  /** Milliseconds between the check polls over the open peer; `0` sends none. */
  checkIntervalMs?: number;
  /** Headers added to every subscribe, unsubscribe and poll call, read per call. */
  headers?: Record<string, string> | (() => Record<string, string>);
}

const DEFAULT_CHECK_INTERVAL_MS = 60000;
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `basePath` | `string` | — | The path of the BE's events viewset. One trailing slash is stripped. |
| `peer` | `EventPeerLike` | — | A muxws `Peer`. Carries pushes, and the client's calls while it is [open](#the-peer). The client registers one `onClose` and one `onReconnect` handler on it. |
| `axios` | `EventAxiosLike` | — | An `AxiosInstance`, or the `axios` module. Carries `poll` while there is no open peer. |
| `pollSettings` | [`PollSettings`](#pollsettings) | `DEFAULT_POLL_SETTINGS` | The constants of the polling ladder. |
| `draw` | [`Draw`](#draw) | `Math.random` | The jitter draw of the polling ladder. |
| `checkIntervalMs` | `number` | `DEFAULT_CHECK_INTERVAL_MS` (60000) | The interval of the [check poll](#the-peer), jittered by `pollSettings.jitterFraction` and drawn with `draw`. `0`, a negative and a non-finite value send none. |
| `headers` | `Record<string, string> \| () => Record<string, string>` | — | Headers of every `subscribe`, `unsubscribe` and `poll` call: stream headers over the peer, request headers over axios. A function is called once per call. The BE resolves the scope of a [scoped topic](#scoped-topics) from the calling request. |

The client is registered for its `peer` until [`close()`](#eventclient); `eventsOf(peer)` returns it.

| Throws `Error` | When |
|----------------|------|
| `createEventClient needs a peer, axios, or both` | Neither `peer` nor `axios` is given |
| `This peer already has an event client` | `peer` has a registered client that is not closed |

## eventsOf

```ts
function eventsOf(peer: object): EventClient | undefined;
```

The client registered for `peer`, or `undefined`. A muxws ViewSet's `watch()` uses it when the
ViewSet's options carry no `events`.

## EventClient

```ts
interface EventClient {
  readonly basePath: string;
  subscribe<P>(topic: string, event: EventDefinition<P>, handlers: EventHandlers<NoInfer<P>>): EventSubscription;
  close(): void;
  refresh?(topic?: string): void;
}
```

| Member | Description |
|--------|-------------|
| `basePath` | `EventClientOptions.basePath` without its trailing slash. |
| `subscribe(topic, event, handlers)` | Subscribes `handlers` to the events named `event.name` on `topic`. The client holds one server subscription and one rev per topic, and counts the subscriptions on it: the first subscription of a topic sends `subscribe` over an open peer, or adds the topic to the next poll; a further one sends nothing, and its `ready` resolves at once when the topic already holds a rev. `P` is read off `event` only. Throws `Error('The event client is closed')` after `close()`. |
| `close()` | Clears the client's timer, sends `unsubscribe` for every topic subscribed on an open peer, and for every topic whose `subscribe` is in flight once it is answered on the same socket, rejects every `ready` that has not settled with `Error('The event client is closed')`, ignores every later answer and push, and unregisters the client from its peer, which may then be given a new client. It calls no handler. A second call does nothing. |
| `refresh(topic?)` | Optional member; the client of `createEventClient` has it. For `topic`, or every followed topic, that holds a rev: sends `subscribe` over the open peer, or polls at once over axios without one, so that the BE resolves the topic's scope anew; the topic's pushes are held until the answer. See [Scoped topics](#scoped-topics). A topic without a rev is refreshed once its first answer arrives; after `close()` it does nothing. |

## defineEvent

```ts
function defineEvent<P>(name: string): EventDefinition<P>;

interface EventDefinition<P> {
  /** The BE event's `event_name`. */
  readonly name: string;
}
```

Returns a frozen `{ name }`. `P` is the payload type `onEvent` receives; it exists in the type only.
The client dispatches on the pair (topic, `name`), whatever the event's version, so `P` describes
the payloads of every version a subscription handles: one type whose fields of other versions are
optional, or a union of one type per version.

## EventHandlers

```ts
interface EventHandlers<P> {
  onEvent: (payload: P, meta: EventMeta) => void;
  onResync: () => void;
  onError?: (error: unknown) => void;
}
```

| Handler | Called |
|---------|--------|
| `onEvent` | Required. With the payload and the [`EventMeta`](#eventmeta) of every applied event of the topic whose name is the subscription's, whatever its version, in rev order. |
| `onResync` | Required. When the client cannot tell which events of the topic it missed; see [resync](#push-gap-poll-and-resync). Every subscription of the topic is called, whatever its event name. |
| `onError` | With the error of a refusal of the topic; the subscription is removed. Becomes the initial `EventSubscription.onError`. |

The handlers are function-typed properties, so their parameters are checked strictly: an `onEvent`
whose payload parameter type is not `P`, a type with a field `P` lacks included, or whose `meta`
parameter type is not `EventMeta`, does not compile, and a `defineEvent` without a type argument
has an `unknown` payload. An `onEvent` that declares only the payload parameter compiles. A handler
that throws does not stop the client or the other handlers. Its error is rethrown in a microtask,
where it reaches the environment's uncaught-error reporting.

## EventMeta

```ts
interface EventMeta {
  readonly topic: string;
  readonly rev: number;
  readonly prev: number;
  readonly version: number;
  readonly scope?: string;
}
```

| Field | Value |
|-------|-------|
| `topic` | The topic the event was published to. |
| `rev` | The event's rev on its topic. |
| `prev` | The rev the client held for the topic before it applied the event: the push's `:prev`, or the poll answer entry's `prev`, which chains the events of the answer. |
| `version` | The backend event's `event_version`, an integer of at least 1: the push's `:version` header, or the poll answer entry's `version`. |
| `scope` | The scope key the client holds for a [scoped topic](#scoped-topics); absent for an unscoped topic. |

One frozen object per applied event, passed to every subscription the event is dispatched to.
`version` does not narrow the type of the payload.

## EventSubscription

```ts
interface EventSubscription {
  readonly ready: Promise<void>;
  unsubscribe(): void;
  onError?: (error: unknown) => void;
}
```

| Member | Description |
|--------|-------------|
| `ready` | Resolves once the topic holds a rev: the `subscribe` over the peer is answered, or a poll answered the topic. Rejects with the error of a refusal that comes first, with the subscription removed, and with `Error('The event client is closed')` when [`close()`](#eventclient) comes first. A rejection nobody awaits is not reported as unhandled. |
| `unsubscribe()` | Removes the subscription. The last subscription of a topic ends the client's following of it: over an open peer with an `unsubscribe` call, sent once a `subscribe` still in flight is answered; otherwise the topic is left out of the next poll. A second call does nothing. |
| `onError` | Called with the error of the refusal that removes the subscription. Writable; initially `EventHandlers.onError`. |

A refusal is reported with:

| Refusal | Error |
|---------|-------|
| `subscribe` or `poll` answered over the peer with a [refusing status](#calls) | `ViewSetRequestError(status, body, headers)` |
| `poll` answered over axios with a refusing status | the `AxiosError` axios throws |
| a topic of a poll answer carries `error` | `ViewSetRequestError` with `response.data` `{ detail, detail_code: error, detail_params: { topic } }` and the answer's `status`, or for an answer without one the status below |

| Topic `error` | `response.status` without `status` |
|---------------|-------------------|
| `event_topic_invalid` | 422 |
| `event_topic_refused` | 403 |
| `event_scope_missing` | 403 |
| `not_authorized` | 403 |
| `session_expired` | 401 |
| `rate_limited` | 429 |
| any other code | 403 |

A status of 429 or of 500 and above is not a refusal: the topic stays, is polled on the next
rung, and a failed `subscribe` of it is sent again (see [Calls](#calls)). `onError` is not called
and `ready` does not settle for it.

A poll refused with a status refuses every topic it carried.

## Calls

The client sends three calls, each as one POST to `{basePath}/{name}`:

| Call | Body | Answer | Sent |
|------|------|--------|------|
| `subscribe` | `{ topic }` | `{ rev, scope? }` | Over an open peer only: for the first subscription of a topic, for every followed topic on `onReconnect`, on `refresh()`, after a poll answered a subscribed topic under another scope, and on every rung for a topic whose `subscribe` failed without refusing |
| `unsubscribe` | `{ topic }` | — | Over an open peer only, for a topic subscribed on its socket: on the last subscription's `unsubscribe()`, on a refusal of the topic, and on `close()`, also once a `subscribe` in flight at `close()` is answered. Its failure is not reported |
| `poll` | `{ cursors: { [topic]: rev \| null }, scopes: { [topic]: scope } }`, `scopes` holding the topics of the poll with a scope (`{}` for none) | `{ topics: { [topic]: { rev, events, error?, status?, scope? } }, poll_after_ms }`, each event `{ rev, prev, event, version, payload }` | Over an open peer, otherwise over axios |

Over the peer a call is one stream, `peer.open(body, { headers: { ...headers, ':method': 'POST',
':path': path }, end: true })`, and its status is the reply's `:status` header, 200 when absent. Over
axios it is `axios.post(path, body, { headers })`, or `axios.post(path, body)` without the `headers`
option. `headers` is `EventClientOptions.headers`, read once per call. No call carries
`Idempotency-Key`, and no call is repeated by the [repeat machinery](./idempotency#frontend) of the
proxies.

The `subscribe` and `unsubscribe` calls of one topic on one peer are sent one at a time, whichever
client of the peer sends them: a call is opened once the one before it is answered. The backend
serves the streams of a socket concurrently, and an `unsubscribe` answered after a later
`subscribe` of the topic would end that subscription. A new client of a peer therefore subscribes a
topic once the closed client's `unsubscribe` of it is answered.

A failure with a status from 400 to 499 refuses, except 429. 429, every status of 500 or above, and
a failure without a status (`ConnectionLost`, a network error) are not reported: a failed rung `poll`
is sent again on the next rung, a failed gap `poll` leaves the topic to the ladder (see
[below](#push-gap-poll-and-resync)), and a failed `subscribe` is sent again on each rung of the
[ladder](#polling-ladder) while the peer is open, and on the next reconnect, with the topic polled
meanwhile; the subscription stays. A `subscribe` sent again that is refused reports the refusal as
any other.

## Push, gap, poll and resync

Per topic the client holds `rev`, the rev of the last event it applied or of the first answer, and
`null` before that answer.

| Arrival | Condition | Action |
|---------|-----------|--------|
| push or polled event | `prev` equals the held rev | `onEvent` on every subscription of the topic with the event's name, whatever its version; the held rev becomes the event's `rev` |
| push or polled event | `rev` at most the held rev | ignored |
| push | `rev` above the held rev, any other `prev` | one `poll` of that topic from the held rev |
| push | the topic holds no rev yet | held, and replayed by these rules once the first answer sets the rev |
| subscribe or poll answer | the topic holds no rev yet | the held rev becomes the answer's `rev`; `ready` resolves |
| poll answer | `events` is an array | each event as above; an event whose `prev` is below the held rev and whose `rev` is above it is applied as one whose `prev` equals the held rev. After the events, the held rev becomes the answer's `rev` when that is larger: the answer leaves out the events the BE's delivery filters refuse, and its `rev` covers them |
| poll answer | `events: null` | `onResync` on every subscription of the topic; the held rev becomes the answer's `rev` |
| poll answer | `rev` below the cursor the poll carried | `onResync`; the held rev becomes the answer's `rev` |
| poll answer | an event in `events` that is malformed (without a `rev` or `prev` that is a number or a string of one, a string `event`, or a `version` that is an integer of at least 1 or a string of one), or is neither applied nor ignored | `onResync`; the held rev becomes the answer's `rev` |
| subscribe answer | `rev` at most the held rev | nothing; the topic resumes without a read |
| subscribe answer | `rev` above the held rev | one `poll` of that topic from the held rev |
| gap poll | fails without refusing the topic, or its answer does not answer the topic | the ladder runs, also over an open peer, and polls the topic until a poll answers it |
| any | a topic the client does not follow | ignored |
| push | the topic is scoped, holds a rev, and `headers()` changed since the topic's last answer | `refresh(topic)`, and the push is held |
| push | `:scope` differs from the scope the topic holds | ignored |
| push | the topic is refreshing | held until the answer |
| subscribe or poll answer | the topic holds no rev yet | the topic also takes the answer's `scope`; held pushes of another scope are dropped |
| subscribe or poll answer | `scope` differs from the held one | `onResync`; the held rev and scope become the answer's; held pushes of the new scope above that rev are received again; a poll answer also sends `subscribe` for a topic subscribed on the open socket |
| subscribe or poll answer | the topic is refreshing, same `scope` | the answer as above, then the held pushes are received again |
| refresh `subscribe` | fails without refusing | the held pushes are dropped; the ladder polls the topic and sends `subscribe` again |

A topic answer without a numeric `rev` and without `error` is ignored. `events: null` is not a
resync when pushes applied while the poll was in flight moved the held rev to at least the answer's
`rev`. A push whose headers lack `:topic`, `:event`, a `:rev` or `:prev` that is a number or a
string of one, or a `:version` that is an integer of at least 1 or a string of one, is ignored.

A gap poll is sent at once, over the open peer or over axios, and not when there is neither. A gap
that arrives while a gap poll of the topic is in flight is polled again after it, when the held rev
has not reached that gap's `rev` by then. A gap poll is not sent for a topic the client no longer
follows, also when a handler of a held push replayed before it ended the following.

## Scoped topics

A [scoped viewset topic](../guide/events#scoped-topics) answers `scope`, the key the BE resolved
from the calling request, with every `subscribe` and poll answer, and carries it in each push as
`:scope`. The client holds it per topic, applies only pushes of the held scope, sends it in the
`scopes` of every poll, and resyncs a topic answered under another scope (see the table above).
`refresh(topic?)` asks the BE to resolve the scope anew. The first answer of a scoped topic, sent
with headers that differ from `headers()` read on its arrival, refreshes the topic. A topic without
`scope` in its answers is
unscoped and keeps the rules above unchanged.

## The peer

| Peer state | The client |
|------------|------------|
| open, every followed topic subscribed on its socket | holds the ladder; sends a check poll once per `checkIntervalMs` |
| open, a topic not subscribed on its socket (a `subscribe` in flight, or one that failed without refusing), or a topic whose gap poll or check poll failed without refusing or left the topic unanswered | runs the ladder over the peer; each rung sends `subscribe` again for a topic whose `subscribe` failed |
| `onClose` fired | drops every socket subscription and resumes the ladder on its base rung over axios; without axios it waits for `onReconnect` |
| `onReconnect` fired | sends `subscribe` for every followed topic, runs the ladder over the peer until every one is answered, then holds it |

A check poll is one `poll` over the open peer of every topic that is subscribed on its socket, holds
a rev, and has no gap poll in flight, sent while the ladder is held. It closes a gap the next push
would reveal, when no next push comes: a push the BE lost on its way to the socket. Its answer is
applied as a rung's. A topic the answer does not answer, and every topic of a check that fails
without refusing them, start the ladder until a poll answers them; a refusal refuses the topics.
The check timer runs while the client has a peer and follows a topic; a tick on which the peer is
not open or the ladder runs sends nothing.

A `subscribe` refused on the new socket refuses the topic. An answer from a socket that has since
closed is dropped. The peer counts as open while `peer.isOpen` is not `false`; a peer without
`isOpen` counts as open until `onClose` and again from `onReconnect`.

## attachEvents and handleEventStream

```ts
function attachEvents(peer: EventPeerLike, fallback?: EventStreamHandler): void;
function handleEventStream(peer: object, payload: unknown, stream: EventStreamLike): boolean;
```

`attachEvents` installs `peer.onStream`: a stream whose headers carry `:event` goes to the peer's
client, any other stream to `fallback`, or is reset with `REFUSED` (4) and the reason
`no handler for this stream` without one. A muxws peer has one handler slot, and `attachEvents`
takes it. Throws `Error('attachEvents needs a peer with onStream')` for a peer without `onStream`.

`handleEventStream` is for a handler that owns the slot: it returns `true` for a stream whose
headers carry `:event` as a string, having passed it to `peer`'s client, and `false`, having done
nothing, for any other. An event stream on a peer without a client returns `true` and is dropped.

```ts
peer.onStream(async (payload, stream) => {
  if (handleEventStream(peer, payload, stream)) return;
  await ownHandler(payload, stream);
});
```

## Peer and axios types

```ts
/** A muxws peer as the event client uses it. `onStream` is needed by `attachEvents` only. */
interface EventPeerLike extends MuxwsPeerLike {
  onStream?(handler: EventStreamHandler): void;
}

/** A stream the remote opened. */
interface EventStreamLike {
  readonly headers: Record<string, unknown>;
  reset?(code: number, reason?: string): Promise<void>;
}

type EventStreamHandler = (payload: any, stream: EventStreamLike) => void | Promise<void>;

/** The axios method the client uses; an `AxiosInstance` is one. */
interface EventAxiosLike {
  post<R = unknown>(url: string, data?: unknown, config?: { headers?: Record<string, string> }): Promise<{ data: R }>;
}
```

`MuxwsPeerLike` is in [route_muxws](./route-muxws#peer-types). A muxws `Peer` satisfies
`EventPeerLike`, a muxws `Stream` satisfies `EventStreamLike`.

## records_changed

```ts
/** The payload of the BE's `records_changed` event: one action's changes. */
interface RecordsChanged<T, K extends KeyType = KeyType> {
  op: 'created' | 'updated' | 'deleted';
  changes: Change<T, K>[];
}

/** One changed record. */
interface Change<T, K extends KeyType = KeyType> {
  pk: K;
  /** The record retrieve(pk) answered; set by watch({ fetch: true }), absent otherwise. */
  record?: T | null;
}

const RECORDS_CHANGED: EventDefinition<RecordsChanged<unknown>>;

function applyChanges<T, K extends KeyType = KeyType>(
  rows: readonly T[],
  change: RecordsChanged<T, K>,
  pkFieldName: string,
): T[];
```

`RECORDS_CHANGED` is `defineEvent('records_changed')`, the event
[`watch()`](./vue-mixins#watch) subscribes a ViewSet's topic to (`options.topic`, else the
ViewSet's `topic` option, else its `basePath`). The backend's
`RecordsChanged` is version `1`, and `RecordsChanged<T, K>` is the payload of that version.
`watch()` calls `onChange` for version `1` only and `onResync` for an event of any other version.

`applyChanges(rows, change, pkFieldName)` returns a new array and leaves `rows` unchanged:

- `deleted` removes every row whose `row[pkFieldName]` is a changed pk;
- `created` and `updated` replace every row with the change's pk by the change's `record`, or
  append the record when no row has that pk; a change without `record` is skipped.

Pks compare by their string form, so `7` and `'7'` name one row. Applying one change twice gives
the same rows as applying it once.

## Polling ladder

The cadence of the client's polls, as pure functions: nothing here touches a timer, a clock or a
transport. The event client calls `PollLadder`; the functions are exported for a scheduler of
one's own.

### PollSettings

```ts
interface PollSettings {
  /** The interval while anything is active, and after any rev change. */
  baseIntervalMs: number;
  /** The multiplier applied when no rev changed. */
  backoffFactor: number;
  /** The upper bound of the climb, and the cadence while a dialog is open and while idle. */
  ceilingMs: number;
  /** The jitter, as a fraction either way. */
  jitterFraction: number;
}

const DEFAULT_POLL_SETTINGS: PollSettings = {
  baseIntervalMs: 400,
  backoffFactor: 1.5,
  ceilingMs: 5000,
  jitterFraction: 0.2,
};
```

### PollLadderState

```ts
interface PollLadderState {
  /** Anything polled is active and expected to change; false polls at the ceiling. */
  anyActive: boolean;
  /** A dialog is open on anything polled; polls at the ceiling. */
  anyDialogOpen: boolean;
  /** Any polled rev moved since the previous read. */
  revChanged: boolean;
  /** The previous un-jittered ladder interval, or null on the first poll. */
  previousDelay: number | null;
  /** The server's poll_after_ms; only ever raises the delay. */
  pollAfterMs?: number | null;
}
```

A caller that cannot tell whether anything is active passes `anyActive: true`. The event client
does, with `anyDialogOpen: false`.

### Draw

```ts
/** A uniform draw in [0, 1). */
type Draw = () => number;
```

### ladderInterval

```ts
function ladderInterval(state: PollLadderState, settings?: PollSettings): number;
```

The interval in milliseconds, without jitter and without `pollAfterMs`. The first matching branch
answers:

1. `anyDialogOpen`: `ceilingMs`;
2. `!anyActive`: `ceilingMs`;
3. `revChanged`: `baseIntervalMs`;
4. `previousDelay` is `null` or `undefined`: `baseIntervalMs`;
5. otherwise `min(max(previousDelay, baseIntervalMs) * backoffFactor, ceilingMs)`.

No branch stops polling.

### jitterMultiplier

```ts
function jitterMultiplier(draw?: Draw, settings?: PollSettings): number;
```

`1 + (draw() * 2 - 1) * jitterFraction`, in `[1 - jitterFraction, 1 + jitterFraction)`. `draw`
defaults to `Math.random`.

### nextDelay

```ts
function nextDelay(state: PollLadderState, draw?: Draw, settings?: PollSettings): number;
```

Milliseconds until the next poll:
`max(round(max(ladderInterval(state), advised) * jitterMultiplier(draw)), max(advised, baseIntervalMs))`,
where `advised` is `pollAfterMs` when it is a finite number and `0` otherwise. The result is never
below `pollAfterMs` and never below `baseIntervalMs`.

### PollLadder

```ts
class PollLadder {
  constructor(settings?: PollSettings, draw?: Draw);
  get ladder(): number | null;
  next(state: Omit<PollLadderState, 'previousDelay'>): number;
  reset(): void;
  resume(): number;
}
```

| Member | Description |
|--------|-------------|
| `constructor(settings, draw)` | Defaults: `DEFAULT_POLL_SETTINGS`, `Math.random`. |
| `ladder` | The un-jittered interval the last `next()` or `resume()` set, `null` before the first one. |
| `next(state)` | `nextDelay` of `state` with `previousDelay` set to `ladder`, and remembers the new `ladderInterval` as `ladder`. |
| `reset()` | Sets `ladder` to `null`; the next `next()` answers the base rung. |
| `resume()` | Sets `ladder` to `baseIntervalMs` and returns it jittered, floored at `baseIntervalMs`. It does not consult `ladderInterval`, so an open dialog or an idle state does not delay the first poll after a lost push transport. |

With `DEFAULT_POLL_SETTINGS` and no rev change, successive `next()` calls climb through these
intervals; the delay is the interval jittered by ±20 %, floored at 400 ms and at `pollAfterMs`:

| Rung | Interval (ms) | Delay (ms) |
|------|---------------|------------|
| 1 | 400 | 400–480 |
| 2 | 600 | 480–720 |
| 3 | 900 | 720–1080 |
| 4 | 1350 | 1080–1620 |
| 5 | 2025 | 1620–2430 |
| 6 | 3037.5 | 2430–3645 |
| 7 | 4556.25 | 3645–5468 |
| 8 and later | 5000 | 4000–6000 |

A rev change returns the next interval to 400 ms.
