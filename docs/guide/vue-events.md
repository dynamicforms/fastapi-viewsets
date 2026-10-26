# Events (Vue)

The backend publishes an event to a topic; the frontend follows topics with an event client. The
backend writes every event to its store before it pushes it. A frontend with an open muxws peer
receives each event as a push; a frontend without one, or with a lost one, polls the store. The backend
side, `publish()`, topics, authorizers and `ChangeEvents`, is `fastapi_viewsets.events`. The
reference for everything on this page is [Events (Vue) — API Reference](../api/vue-events).

## Setting up the client

One client per peer, built once per application:

```ts
import axios from 'axios';
import { connect } from 'muxws';
import { attachEvents, createEventClient } from '@dynamicforms/fastapi-viewsets';

const peer = await connect('wss://example.test/ws');
attachEvents(peer);                                                      // the peer's onStream handler
const events = createEventClient({ basePath: '/events', peer, axios });
```

`basePath` is the path the backend's
[`register_events(router, base_path="/events")`](./events#subscribing-and-polling-register-events) serves.
`peer` carries the pushes and, while it is open, the client's own calls. `axios` carries the polls
while there is no open peer. A client with neither throws; a client with `axios` alone polls and
receives no pushes:

```ts
const events = createEventClient({ basePath: '/events', axios });
```

`createEventClient` registers the client for its `peer`, and `eventsOf(peer)` returns it. A second
client for the same peer throws until the first one is closed with `close()`. `close()` ends every
subscription of the client: it unsubscribes its topics on the peer's socket and rejects every
`ready` that has not settled. A new client of the peer subscribes a topic once the closed client's
`unsubscribe` of it is answered.

### The peer's stream handler

The backend sends each push as a one-shot stream the peer did not ask for, and the peer hands it
to its `onStream` handler. `attachEvents(peer)` installs that handler: an event stream goes to the
peer's client, and any other stream is reset with `REFUSED`, or passed to the `fallback` given as
the second argument.

A muxws peer has one `onStream` slot. An application or a library that installs its own handler
composes `handleEventStream` into it instead of calling `attachEvents`:

```ts
import { handleEventStream } from '@dynamicforms/fastapi-viewsets';

peer.onStream(async (payload, stream) => {
  if (handleEventStream(peer, payload, stream)) return;   // true: an event stream, dispatched
  await serveOwnStream(payload, stream);
});
```

## Subscribing

An event is a name and a payload type. `defineEvent` names the backend's `event_name`:

```ts
import { defineEvent } from '@dynamicforms/fastapi-viewsets';

const ImportFinished = defineEvent<{ file: string; rows: number }>('import_finished');

const subscription = events.subscribe('imports:7', ImportFinished, {
  onEvent(payload) { notify(`${payload.file}: ${payload.rows} rows`); },
  onResync() { void reloadImports(); },
  onError(error) { console.warn('imports:7 refused', error); },
});
await subscription.ready;
// ...
subscription.unsubscribe();
```

| Handler | Called |
|---------|--------|
| `onEvent(payload, meta)` | Required. Once per event of the topic with the subscription's event name, whatever its version, in rev order. `meta` is the event's [`EventMeta`](#event-versions) |
| `onResync()` | Required. When the client cannot tell which events of the topic it missed: the subscription re-reads the state it displays |
| `onError(error)` | When the backend refuses the topic; the subscription is removed |

The handlers are typed by the definition: an `onEvent` whose payload parameter type differs from
the `defineEvent` type argument, a wider one included, does not compile.

`ready` resolves once the topic is followed: the `subscribe` over the peer is answered, or the first poll answered the
topic. It rejects when the topic is refused first, with the subscription removed; the error is a `ViewSetRequestError`
whose `response.data` is the server's `ApiErrorBody` and whose `response.data.detail_code` names the reason, such as `fastapi-viewsets.event_topic_refused`,
`fastapi-viewsets.event_topic_invalid`, `fastapi-viewsets.not_authorized` or `fastapi-viewsets.session_expired`, or the
`AxiosError` of a poll axios sent. After `close()` it rejects with `Error('The event client is closed')`.

The client holds one server subscription and one rev per topic, however many subscriptions it
carries. Two subscriptions of one topic, for the same or for different event names, send one
`subscribe`; the second one's `ready` resolves at once when the topic already holds a rev. An
arriving event goes to every subscription of its topic with its event name, whatever its version.
`unsubscribe()` of the last subscription of a topic sends `unsubscribe` over the peer, or leaves the
topic out of the next poll.

A handler is idempotent per record: an application may receive one change twice, from the event
and from the re-read `onResync` started, and ends in the same state either way. A handler that
throws does not stop the client or the other handlers; its error is rethrown in a microtask.

### Event versions

Every event carries the version of its payload's shape, the backend's
[`event_version`](./events#event-versions), `1` unless the event class sets a larger one. The client
dispatches by topic and event name, whatever the version, so one subscription receives every version
of its event and branches on `meta.version`:

```ts
const ImportFinished = defineEvent<{ file: string; rows: number; skipped?: number }>('import_finished');

events.subscribe('imports:7', ImportFinished, {
  onEvent(payload, { version }) {
    const skipped = version >= 2 ? `, ${payload.skipped} skipped` : '';
    notify(`${payload.file}: ${payload.rows} rows${skipped}`);
  },
  onResync() { void reloadImports(); },
});
```

The type argument of `defineEvent` describes the payloads of every version the subscription
handles: one type with the fields of later versions optional, as above, or a union of one type per
version. `meta.version` does not narrow the payload type; the handler narrows it itself. A version
the handler does not know reaches it like any other.

`meta` is a frozen `EventMeta`:

| Field | Value |
|-------|-------|
| `topic` | The topic of the event |
| `rev` | The event's rev |
| `prev` | The rev the client held for the topic before it applied the event |
| `version` | The event's version: the push's `:version` header, or the poll answer entry's `version` |
| `scope` | The scope key of a scoped viewset topic, as the BE resolved it for this client; absent otherwise |

A push without a usable `:version`, an integer of at least 1 or a string of one, is ignored, as a
push without a usable rev is. A poll answer entry without one is malformed, and the topic resyncs.

## Push, gap, poll and resync

Per topic the client holds the rev of the last event it applied. Every event carries its own `rev`
and `prev`: on a push, the rev of the event before it on that topic; in a poll answer, the rev of the
event before it in the answer, the cursor for the first:

| Arrival | Action |
|---------|--------|
| `prev` equals the held rev | applied: `onEvent`, and the held rev becomes the event's `rev` |
| `rev` at most the held rev | ignored |
| a push with a larger `rev` and another `prev` | a gap: one `poll` of the topic from the held rev |
| a poll answer with `events` | each event by the rules above; an event whose `prev` is below the held rev and whose `rev` is above it is applied too. Then the held rev becomes the answer's `rev` when that is larger |
| a poll answer with `events: null`, or with a `rev` below the cursor the poll carried | a resync: `onResync` on every subscription of the topic, and the held rev becomes the answer's |

The backend's [delivery filters](./events#delivery-filters) run on pushes and polls alike. An event
they refuse is neither pushed nor polled; the next push names it as its `prev`, which the client
answers with one gap poll. The poll answer leaves the refused event out, its events chain from the
cursor, and its `rev` covers the refused event, so the topic moves on without a resync and without
another poll.

The backend keeps the last events of each topic in a [log](./events#the-log-and-the-counter) that expires
`settings.viewsets_event_log_ttl` seconds after the topic's last event. A gap of
seconds, a push lost on the way or two pushes that arrived out of order, is answered from the log by
the gap poll and needs no re-read. A tab the browser froze for longer than the log lives comes back
with a cursor the log cannot answer, and resyncs.

A push that arrives before the topic's first answer is held and applied once that answer gives the
topic a rev.

## Scoped topics

The backend may store a viewset topic's events per tenant ([scoped topics](./events#scoped-topics)):
it resolves a scope key from the calling request, its headers, path and session, and answers it with
every `subscribe` and poll of the topic. The client:

- holds the key of the topic's first answer, and applies only pushes whose `:scope` is that key;
  a push held before the first answer under another key is dropped;
- sends every poll as `{ cursors, scopes }`, `scopes` the key it holds for each scoped topic of the
  poll (`{}` for none);
- resyncs a topic answered under another key: `onResync`, and the held rev becomes the answer's. A
  scope change seen by a poll subscribes the topic again on an open socket, so that the backend's
  subscription follows the new key;
- passes the key to `onEvent` in `EventMeta.scope`.

The request the backend resolves the key from carries `headers` of the client's options, read for
every call:

```ts
const events = createEventClient({
  basePath: '/api/v1/events',
  peer,
  axios,
  headers: () => ({ 'X-Org': session.orgSlug }),
});
```

When the tenant changes, the application calls `events.refresh()`, or `refresh(topic)` for one
topic: each answered topic is subscribed again over the open peer, or polled at once without one,
and its pushes are held until the answer. An answer under another key resyncs the topic and applies
the held pushes of the new key above the answer's rev; an answer under the same key applies the held
pushes. A `subscribe` of a refresh that fails without a refusal drops the held pushes and leaves the
topic to the ladder, which sends the `subscribe` again. A topic whose first answer is pending when
`refresh()` is called is refreshed once that answer arrives. The client refreshes a scoped topic
itself when a push arrives after `headers()` changed since the topic was answered, and when its first
answer arrives after `headers()` changed since that answer's call was sent.

```ts
watch(() => session.orgSlug, () => {
  events.refresh?.();
  void reload();
});
```

`refresh` is an optional member of `EventClient`, so an application's own implementation of the
interface keeps compiling.

## Polling

The client polls while a topic it follows has no subscription on an open peer's socket: always on
an `axios`-only client, after the peer is lost, and while a `subscribe` over the peer is
unanswered. Every followed topic goes into one `poll` call per rung:

```json
{ "cursors": { "/api/music": 17, "imports:7": null, "/orgs/7/tags": 12 }, "scopes": { "/orgs/7/tags": "7" } }
```

A topic added without an open peer is polled at once, so its `ready` does not wait for a rung.
Otherwise the rungs follow the polling ladder:

| Rule | Value |
|------|-------|
| base interval, after a poll in which any topic's rev moved, and after a lost peer | 400 ms |
| a poll in which no rev moved | the interval ×1.5 |
| ceiling | 5000 ms |
| jitter | ±20 %, applied once to each delay |
| floor | the delay is never below 400 ms, nor below the answer's `poll_after_ms` |

From a quiet start the intervals are 400, 600, 900, 1350, 2025, 3037.5, 4556.25 and then 5000 ms. The
ladder never stops: a client that followed a quiet topic polls it every 4 to 6 s. The backend's
`poll_after_ms` slows the client down and never speeds it up. The ladder is exported for other
schedulers: `PollLadder`, `nextDelay` and `DEFAULT_POLL_SETTINGS` (see
[Polling ladder](../api/vue-events#polling-ladder)); `pollSettings` and `draw` in the client's
options replace its constants and its jitter draw.

A `subscribe` or `poll` that fails with a status from 400 to 499 refuses the topics it carried, except 429. 429, every
status of 500 or above, and a failure without a status refuse nothing and are sent again: a poll on the next rung, a
subscribe on each rung while the peer is open and on the next reconnect, with the topic polled meanwhile. The
subscription stays, and its `onError` is called only for a refusal. A topic of a poll answer is classified by the
answer's `status`; its `error` is the server's `ApiErrorBody`, the `response.data` of the `ViewSetRequestError` the
topic is refused with. A topic answered with a `status` of 429 or of 500 or above, and an answer whose `error` is not
an `ApiErrorBody` or that has no `status`, stays followed and is polled on the next rung. A
gap poll that fails this way, or whose answer leaves the topic out, starts the ladder, also over an open peer, until a
poll answers the topic.

## Losing and regaining the socket

While the peer is open and every topic is subscribed on its socket, the client sends no poll except
the check poll: once per `checkIntervalMs` (60 s by default) one `poll` of every subscribed topic
over the peer. A gap shows when the next event of the topic arrives, so a lost push of the last
event, dropped between the backend's pub/sub and the socket, is caught only by a poll. When
the peer closes, the backend drops every subscription that socket held; the client resumes the
ladder on its base rung over `axios`, from the revs it holds, so the events published meanwhile
arrive by poll. A client without `axios` waits.

On `onReconnect` the client sends `subscribe` for every topic it follows. An answer whose rev is
at most the one the client holds resumes the topic without a read; a larger one is a gap and polls
from the client's rev. Once every topic has answered, the ladder is held again. A subscribe the
backend refuses on the new socket, for instance after the user's access was withdrawn, calls the
subscription's `onError` and removes it.

### Replacing the peer

A muxws peer reconnects by itself, but it closes for good when the application closes it, for
instance on logout, and the next socket is a new peer. `peer` may be a function that returns the
current peer, or a promise of it, like the peer source of a muxws ViewSet; one client then lives as
long as the application and follows its topics across peers:

```ts
async function currentPeer(): Promise<Peer> {
  const peer = await connect('wss://example.test/ws');   // or the application's shared socket
  attachEvents(peer);
  return peer;
}

const events = createEventClient({ basePath: '/events', peer: currentPeer, axios });
```

The client calls the function on its first subscription. When the peer it returned closes for good
(`onClose` with `willRetry: false`), the client unregisters from it, polls over `axios` from the revs
it holds, and calls the function again on the ladder's next rung; a function that throws or rejects
is called again on each later rung, also without `axios`. Every followed topic is subscribed on the
socket of the new peer, and the client is registered for it, so `eventsOf(peer)` returns it. The
function attaches the stream handler (`attachEvents` or `handleEventStream`) to the peers it
returns. A peer whose `isClosed` is `true` is not adopted, and the function is called again on the
next rung. A peer that already has another client is not adopted either: the client reports
`Error('This peer already has an event client')` outside its call stack and calls the function again
on the next rung.

A ViewSet's `watch()` finds the client by its peer only once the client has adopted that peer; a
ViewSet built on the same peer source passes the client in its `events` option.

## `watch()` on a ViewSet

Every ViewSet has `watch(options)`. It subscribes the ViewSet's topic to `records_changed`, the
event the backend's `ChangeEvents` middleware publishes after each create,
update and destroy action, and returns the `EventSubscription`. The topic is `options.topic`, else
the ViewSet's `topic` option, else its `basePath`. The topic is the backend path relative to the
application's `root_path`, without a Starlette `Mount` prefix, so a ViewSet whose `basePath` carries
such a prefix sets `topic`:

```ts
const tracks = new TrackRestViewSet({ basePath: '/backend/api/music', topic: '/api/music', events });
const watching = tracks.watch({
  fetch: true,
  onChange(change, meta) { /* change.op, change.changes; meta.version */ },
  onResync() { /* re-read */ },
  onError(error) { /* refused */ },
});
```

The client is `events` from the ViewSet's options. Without it, a muxws ViewSet uses
`eventsOf(peer)`, the client created for its peer, and a REST ViewSet throws from `watch()`:

```ts
const tracks = new TrackRestViewSet({ basePath: '/api/music', events });
```

The payload is `RecordsChanged<T, K>`: `op` is `created`, `updated` or `deleted`, and `changes`
holds one `{ pk, record? }` per record the action touched. The backend sends the pk only. With
`fetch: true` every `created` and `updated` change gets `record`, the answer of `retrieve(pk)` in the
endpoint's format and the reader's context, before `onChange`. The retrieve is issued inside
`inBackground`, so call activity reports it with `background: true` and a busy indicator that counts
foreground calls leaves it out; a `retrieve` override that issues its request after an `await` is
not marked. A change whose retrieve answers 404,
a row that is gone or the caller may no longer see, leaves the event and follows it in a second
`onChange` with `op: 'deleted'`, so a displayed row is removed. `onChange` and `onResync` are
called in the order their events arrived, also while `fetch` waits for a retrieve. `onChange`
receives the event's `EventMeta` as its second argument. `RecordsChanged` is the payload of
`records_changed` version `1`, and `watch()` handles that version only: an event of any other
version calls `onResync` instead of `onChange`, with or without `fetch`. A frontend that reads a
later version subscribes to `records_changed` with an event definition of its own. After
`unsubscribe()` neither is called, also for an event whose retrieve is still in flight. The options
are in [`watch()`](../api/vue-mixins#watch).

`fetch` also takes a function, `(pks) => Promise<rows>`. `watch()` calls it once per `created` or
`updated` event with the pks of the changes that carry no `record`, inside `inBackground`, and
matches the rows it returns to the changes by the ViewSet's `pkFieldName`; a pk it returns no row for
is gone and follows in the `deleted` `onChange`, as a 404 does for `fetch: true`. A rejection is
passed to `onError` and the event ends in `onResync`. Here the BE filter model declares `id` with
the `in` operator:

```ts
const watching = members.watch({
  fetch: (pks) => members.list({ id__in: pks }),
  onChange(change) { rows.value = applyChanges(rows.value, change, members.pkFieldName); },
  onResync() { void reload(); },
});
```

One list call per event reads the rows the way the list endpoint shows them, under the same
`get_queryset` scoping; a row the list no longer shows to the reader is removed.

With a `fetch` function, the changes are typed with the rows that function returns, so a grid that
shows a list type watches by fetching with the same `listType`:

```ts
members.watch({
  fetch: (pks) => members.list({ id__in: pks }, { listType: 'compact' }),
  onChange(change) { compactRows.value = applyChanges(compactRows.value, change, members.pkFieldName); },
  onResync() { void reload(); },
});
```

Without a `fetch` function, the changes are typed with `R`, the row type of a list call without
`listType` ([`restViewSet<T, L, V, R>()`](./vue-mixins#list-types)). `fetch: true` reads records with
`retrieve`, so it compiles only when `R` is `T`.

### A grid that holds every row

`applyChanges(rows, change, pkFieldName)` returns `rows` with the change applied: `deleted` removes
the rows with the changed pks, `created` and `updated` replace the row with the same pk or append
the record. A grid that holds the whole data set and sorts and filters it locally needs nothing
else:

```ts
import { onMounted, onUnmounted, ref } from 'vue';
import { applyChanges, BulkViewSetMixin, muxwsViewSet } from '@dynamicforms/fastapi-viewsets';

class TrackViewSet extends muxwsViewSet<Track>()('id', [BulkViewSetMixin]) {}

const tracks = new TrackViewSet({ basePath: '/api/music', peer });
const rows = ref<Track[]>([]);

let loaded: Promise<void> = Promise.resolve();
function reload(): Promise<void> {
  loaded = tracks.list().then((all) => { rows.value = all; });
  return loaded;
}

const watching = tracks.watch({
  fetch: true,
  onChange(change) {
    void loaded.then(() => { rows.value = applyChanges(rows.value, change, tracks.pkFieldName); });
  },
  onResync() { void reload(); },
});

onMounted(async () => {
  await watching.ready;
  await reload();
});
onUnmounted(() => watching.unsubscribe());
```

The subscription is made before the first read, so no change published between the read and the
subscription is lost. `onChange` applies each change after the load in flight, so a load answered
after the event does not overwrite it; a change the load already contains is applied a second time
with the same result.

### A grid that pages on the server

A grid that shows one page of a sorted, filtered server list cannot place a change itself. It
re-reads its page on every event and on every resync:

```ts
import { muxwsViewSet, PaginatedListMixin, type PageParams, type PaginatedList } from '@dynamicforms/fastapi-viewsets';

class TrackPageViewSet extends muxwsViewSet<Track>()('id', [PaginatedListMixin]) {}

const pages = new TrackPageViewSet({ basePath: '/api/music', peer });
const page = ref<PaginatedList<Track> | null>(null);
const params = ref<PageParams>({ offset: 0, limit: 50, sort: 'title:asc' });

async function loadPage(): Promise<void> {
  page.value = await pages.listPage(params.value);
}

const watching = pages.watch({
  onChange() { void loadPage(); },
  onResync() { void loadPage(); },
});
onUnmounted(() => watching.unsubscribe());
```

`fetch` is left `false` here: the re-read of the page carries the records.
