# Events API

Status: concept. Nothing described here is implemented. The implementation plan is the last section.

## Purpose

A viewset answers when the frontend asks. The events API carries the other direction: the backend
publishes an event to a topic, and every frontend following that topic learns of it.

The delivery model is the one taskwire uses. **The store is the truth; a push is an accelerator.**
Every event is recorded in a store before it is pushed anywhere, a frontend that holds a muxws
socket receives it as a push, and a frontend without one, or with a lost one, polls. A deployment
with no socket is a complete deployment, with poll latency.

The library supplies:

- the publish side: `Event` classes, topic sources, `publish()`, and a store that reaches every
  web process;
- the subscribe side: a viewset with `subscribe`, `unsubscribe` and `poll`, an authorizer list, a
  delivery filter list, and delivery to each subscribed socket;
- one plugin, `ChangeEvents`: a command middleware that publishes `RecordsChanged` after every
  create, update and destroy action of a viewset, globally or per viewset;
- the frontend counterpart: an event client with push and poll, the polling ladder, `watch()` on
  every ViewSet, and `applyChanges()` for a list of rows.

Everything else, which events exist, which topics address them and what a frontend does with them,
is the application's.

## Vocabulary

| Term | Meaning |
|---|---|
| **event** | A pydantic model with a class-level `event_name`. The name and the fields are the same on both sides. |
| **topic** | A string that names a stream of events. Either a viewset's routed base path (`/api/music`) or a declared source's name with an optional key (`imports`, `imports:7`). Matching is exact. |
| **source** | A declared owner of custom topics. Declared once per name; a second declaration raises. |
| **rev** | The integer the store assigns to each event of a topic: `max(previous rev + 1, now in milliseconds)`. Strictly increasing per topic, and larger than every rev issued before the counter was lost or dropped. |
| **prev** | The rev an event replaced: the topic's rev before it, `0` for a topic without one. Carried by every event. |
| **log** | The last `viewsets_event_log_size` events of a topic, kept by the store for `viewsets_event_log_ttl` seconds for `poll`. |
| **subscription** | One (socket, topic) pair, held by the web process that holds the socket. |
| **authorizer** | An application function that decides whether a caller may follow a topic. |
| **delivery filter** | An application function that decides, per socket and per event, whether a push is sent. |

## Rules

1. **An event is published to one topic.** `publish()` with an invalid topic raises `ValueError`.
2. **The store is written before anything is pushed.** `rev` and `prev` are assigned by the store;
   the push and the poll answer carry the same pair.
3. **A topic is admitted by the authorizers.** Each answers `True`, `False` or `None`; the first
   `True` or `False` decides; no decision refuses. The authorizers run on every `subscribe` and on
   every `poll`, per topic.
4. **A push is best effort.** `publish()` never raises for a delivery failure. A push that fails or
   exceeds `settings.viewsets_event_push_timeout` is logged at debug level and dropped.
5. **A subscription lives as long as its socket.** A lost socket drops every subscription it held.
   A reconnected peer is a new socket and subscribes again.
6. **A frontend applies an event whose `prev` is the rev it holds.** Any other `prev` with a
   larger `rev` is a gap, answered by one `poll`; a `poll` the log cannot answer is a resync,
   `onResync()`, where the frontend re-reads the state it displays. A `rev` at most the held one is
   ignored.
7. **A handler is idempotent per record.** A frontend may apply one change twice, once from the
   event and once from a re-read, and ends in the same state either way.
8. **Polling may only be slowed down.** `poll_after_ms` in a poll answer is a floor on the client's
   interval, never a ceiling.

## Topics and naming

Two syntaxes, which cannot collide:

| Topic | Form | Declared by | Example |
|---|---|---|---|
| viewset | the routed base path, starting with `/` | `route_viewset`, mounted by `include_router` | `/api/music` |
| custom | `name` or `name:key`; `name` matches `[a-z0-9_.-]+` and is unique in the process | `source(name)` | `imports`, `imports:7` |

A viewset's topic is the path a request to it is routed by, prefix included, which is the
`basePath` the frontend ViewSet already holds. One viewset mounted twice has two topics.

```python
from fastapi_viewsets.events import Event, source


class ImportFinished(Event):
    event_name = "import_finished"

    file: str
    rows: int


imports = source("imports", authorizer=club_member)

await imports.publish(ImportFinished(file="members.csv", rows=412))            # topic "imports"
await imports.publish(ImportFinished(file="members.csv", rows=412), key="7")   # topic "imports:7"
```

`source(name, authorizer=None)` registers the name in a process-wide registry and raises
`ValueError` on a second declaration of the same name or on a name that does not match the form.
`authorizer` is an `EventAuthorizer` (below) consulted by `DeclaredSources()` for the source's
topics. `Source.publish(event, key=None)` is `publish(f"{name}:{key}", event)` with the key, or
`publish(name, event)` without. `publish(topic, event)` is also public, for a topic the caller
composed itself; it validates the topic's form and nothing else. `Source.drop(key)` removes the
topic's counter and log from the store, for a keyed topic that has no further events.

`event_name` is unique within a source, not globally: the frontend dispatches on the pair (topic,
event name). `records_changed` is the library's event on viewset topics.

## Backend

### Events

`Event` is a pydantic `BaseModel`. `event_name` is a `ClassVar[str]`; a subclass that does not set
it raises `TypeError` when the class is created. The payload on the wire is `model_dump(mode="json")`.

`publish(topic, event)`:

1. validates `topic`;
2. `rev, prev = await store.append(topic, event.event_name, payload)`, which assigns the pair and
   keeps the event in the topic's log;
3. `await store.publish(topic, rev, prev, event.event_name, payload)`, which reaches `deliver()` in
   every process that watches the topic.

It publishes immediately. An event that describes a database change is published after the
transaction commits, otherwise a frontend reacting to it reads the state from before the change.
`publish()` may be called from any process that has a store: a request handler, a Celery worker, a
management command.

### The store

```python
class EventStore(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def append(self, topic: str, name: str, payload: Any) -> tuple[int, int]: ...
    async def revs(self, topics: list[str]) -> dict[str, int]: ...
    async def since(self, topic: str, after: int) -> list[LoggedEvent] | None: ...
    async def publish(self, topic: str, rev: int, prev: int, name: str, payload: Any) -> None: ...
    async def drop(self, topic: str) -> None: ...
    async def watch(self, topic: str) -> None: ...
    async def unwatch(self, topic: str) -> None: ...
```

| Method | `MemoryEventStore` | `RedisEventStore(redis_client, key_prefix="events:")` |
|---|---|---|
| `append` | sets the counter to `max(previous + 1, now_ms)`, appends to a bounded list | one Lua script: the same on `{prefix}rev:{topic}`, which has no expiry, and `LPUSH` + `LTRIM` on `{prefix}log:{topic}` with `EXPIRE viewsets_event_log_ttl` |
| `drop` | deletes the counter and the list | `DEL` on both keys |
| `revs` | reads the counters; an unknown topic is `0` | `MGET`; an absent key is `0` |
| `since` | `[]` when the topic's rev is `after`; otherwise the kept entry with `prev == after` and every newer one; `None` when no kept entry has that `prev` | `LRANGE`, the same rule |
| `publish` | calls `deliver()` in the same process | `PUBLISH {prefix}ch:{topic}`; the reader calls `deliver()` |
| `watch` / `unwatch` | no-op | `SUBSCRIBE` / `UNSUBSCRIBE` on the reader connection |
| `start` / `stop` | no-op | opens and closes the reader task |

`LoggedEvent` is `(rev, prev, name, payload)`. With `viewsets_event_log_size = 0` the log is empty
and every `since` behind the current rev answers `None`.

The log expires, the counter does not. The log lives `viewsets_event_log_ttl` seconds after the
topic's last event: a frontend that did not follow the topic for longer than that holds no cursor
the log can answer, and reloads. The counter stays, so that on a topic with sparse events a
frontend holding the last rev receives the next event with `prev` equal to it and applies it
without a read. A counter that is lost anyway, a `MemoryEventStore` whose process restarted, a
flushed Redis, a dropped topic, restarts at `now_ms`, which is larger than every rev it issued,
and the first event after it carries `prev: 0`: no frontend holds `0`, so every one of them polls,
finds no entry with its `prev`, and resyncs. The counter needs no reset: it is a millisecond
timestamp that advances by at most one per event, and reaches `2^53` in about 285,000 years.

`settings.viewsets_event_store` holds the store; `None` means `MemoryEventStore()`. The application
calls `start_events()` and `stop_events()` from its lifespan, and `check_events()` logs a warning
when a `RedisEventStore` is configured and no reader runs; the message names the missing call.

A process watches a topic while it holds at least one subscription for it. A `publish()` from a
process that holds no socket, a Celery worker, reaches the sockets through Redis and in no other
way: with `MemoryEventStore` in a worker, `publish()` records the event and delivers it to nobody,
and a frontend learns of it on its next poll of that process's store, which is a different store.
A multi-process deployment configures `RedisEventStore` everywhere.

`deliver(topic, rev, prev, name, payload)` is the one entry point into a process's subscriptions.
Both stores call it; nothing else does.

### Subscribing and polling

The subscribe surface is a viewset, so the application's command middleware, context processors and
`/schema` apply to it:

```python
from fastapi_viewsets.events import register_events

register_events(router, base_path="/events")
```

| Call | Transport | Request body | Answer |
|---|---|---|---|
| `POST {base_path}/subscribe` | muxws only | `{"topic": "/api/music"}` | 200 `{"rev": 17}`, the topic's current rev |
| `POST {base_path}/unsubscribe` | muxws only | `{"topic": "/api/music"}` | 204, also for a topic the socket does not hold |
| `POST {base_path}/poll` | both | `{"cursors": {"/api/music": 17, "imports:7": null}}` | 200, below |

`subscribe` and `unsubscribe` carry `@transports(rest=False)`: a subscription names a socket, and a
REST request has none. The endpoint reads the socket from `request.scope["fastapi_viewsets.peer"]`,
which `process_command` sets from its `peer` argument:

```python
@app.websocket("/ws")
async def muxws_endpoint(websocket: WebSocket) -> None:
    peer = await accept(websocket)
    peer.on_stream(lambda payload, stream: process_command(payload, stream, connection=websocket, peer=peer))
    await peer.serve()
```

A `subscribe` served without `peer=` raises `RuntimeError` naming the missing argument; the caller
receives a 500 and the log says what to add.

Subscriptions are held per process in a table keyed by topic and socket. The first subscription of
a topic in a process calls `store.watch(topic)`; the last unsubscribe calls `store.unwatch(topic)`.
The first subscription of a socket installs one `peer.on_close` handler, which drops every
subscription of that socket. A socket holds at most `settings.viewsets_event_max_subscriptions`
topics; one more is refused with 409.

`poll` answers every cursor in one response:

```json
{
  "topics": {
    "/api/music": {"rev": 19, "events": [{"rev": 18, "prev": 17, "event": "records_changed", "payload": {}}, {"rev": 19, "prev": 18, "event": "records_changed", "payload": {}}]},
    "imports:7": {"rev": 3, "events": []},
    "club:9": {"rev": null, "events": null, "error": "event_topic_refused"}
  },
  "poll_after_ms": 0
}
```

Per topic: the authorizers run; a refused topic answers `error` and nothing else, and the other
topics are unaffected. A `null` cursor answers the current rev and no events: the caller starts
from now. A cursor equal to the current rev answers `[]`. A cursor behind it answers
`store.since(topic, cursor)`: the chain of events from the cursor, or `events: null` when the log
no longer holds an event with `prev` equal to it, which the frontend answers with a resync. `poll_after_ms` is
`settings.viewsets_event_poll_after_ms`.

A poll carries at most `settings.viewsets_event_max_subscriptions` cursors; more is 422.

### Authorization

```python
EventAuthorizer = Callable[[Request, Context, str], Awaitable[bool | None]]
```

```python
from fastapi_viewsets.conf import settings
from fastapi_viewsets.events import DeclaredSources, ViewsetTopics

settings.viewsets_event_authorizers = [ViewsetTopics(), DeclaredSources()]
```

The authorizers run in list order with the calling request, its context and the topic, on every
`subscribe` and on every `poll`, per topic. `None` means no opinion. The first `True` admits the
topic, the first `False` refuses it, and no decision refuses it. An `HTTPException` an authorizer
raises refuses the topic with that status on `subscribe`, and with that status's error code in the
topic's `error` on `poll`.

`ViewsetTopics()` admits a viewset's topic to a caller the viewset itself would admit:

- A topic that does not start with `/` gets `None`.
- The viewset is found on `request.app.routes`: the route whose endpoint carries
  `__viewset_class__` and `__viewset_action__` of `list_items`, or of `retrieve` on a viewset
  without `list_items`, and whose `path` is the topic followed by that action's relative path from
  `cls.__viewset_metadata__["paths"]`. No such route gets `None`.
- For that action the authorizer resolves the viewset's action configuration, rebinds the calling
  context to it, `Context(context.raw(), action_configuration=..., action_name=...)`, and runs
  `depends()` of every effective middleware, as `route_viewset` does before the action. An
  `HTTPException` propagates: `Session` answers 401, `Authorization` 403. `RateLimiter` counts the
  call as one call of that action. No exception admits the topic.

`DeclaredSources()` admits a custom topic through its source's `authorizer`; a source without one,
or a name no source declared, gets `None`.

`allow_all` and `allow_authenticated` are shipped as plain functions. `allow_authenticated` admits
a topic when `await context.user` is truthy.

### Delivery

For each subscription of the topic in this process, `deliver()` runs the delivery filters and sends
one one-shot push:

```python
await peer.notify(payload, headers={":event": name, ":topic": topic, ":rev": rev, ":prev": prev})
```

bounded by `settings.viewsets_event_push_timeout` seconds. `ConnectionLost` drops the socket's
subscriptions; any other exception is logged at debug level. Each push is its own task, so a slow
socket does not delay the others.

```python
DeliveryFilter = Callable[[Subscription, str, int, str, Any], bool | Awaitable[bool]]
```

`settings.viewsets_event_delivery_filters` is a list; a filter receives the subscription (`peer`,
`context`, `topic`), the topic, the rev, the event name and the payload, and a `False` skips that
socket. A filter that raises is `True`: the filter is a display decision after the authorizers
have decided access, and a broken one degrades to sending more. The list is empty by default.

An event stream is never answered. On the wire it is one `open` frame with `end` set, which is what
`notify()` sends.

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `viewsets_event_store` | `None` | The `EventStore`; `None` is `MemoryEventStore()` |
| `viewsets_event_authorizers` | `[]` | The authorizer list; empty refuses every topic |
| `viewsets_event_delivery_filters` | `[]` | The delivery filter list |
| `viewsets_event_log_size` | `32` | Events kept per topic for `poll` |
| `viewsets_event_log_ttl` | `60.0` | Seconds a topic's log lives after its last event (`RedisEventStore`; `MemoryEventStore` prunes on access) |
| `viewsets_event_push_timeout` | `2.0` | Seconds one push to one socket may take |
| `viewsets_event_max_subscriptions` | `256` | Topics one socket may hold, and cursors one poll may carry |
| `viewsets_event_poll_after_ms` | `0` | The floor advertised to polling clients |

### Errors

| Code | Status | Raised when |
|---|---|---|
| `event_topic_invalid` | 422 | The topic matches neither form |
| `event_topic_refused` | 403 | No authorizer admitted the topic |
| `event_subscriptions_exceeded` | 409 | The socket already holds `viewsets_event_max_subscriptions` topics |
| `event_cursors_exceeded` | 422 | A poll carries more cursors than `viewsets_event_max_subscriptions` |

## The `ChangeEvents` plugin

```python
from fastapi_viewsets.conf import settings
from fastapi_viewsets.events import ChangeEvents, ViewsetTopics

settings.viewsets_command_middleware = [Idempotency(...), ChangeEvents(), Session(), ...]
settings.viewsets_event_authorizers = [ViewsetTopics()]
```

`ChangeEvents` is a command middleware. Its `__call__`, in the web process, reads the topic off the
matched route and stores it in the context; its `execute()`, where the action executes, publishes
after the action has returned:

| `execution.action` | `op` | `changes` |
|---|---|---|
| `create` | `created` | the returned record |
| `bulk_create` | `created` | each returned record |
| `update`, `partial_update` | `updated` | the returned record |
| `bulk_update`, `bulk_partial_update` | `updated` | each returned record |
| `destroy` | `deleted` | `kwargs["pk"]` |
| `bulk_destroy` | `deleted` | each pk of `kwargs["pk"]` |

Any other action publishes nothing. An action that raises publishes nothing.

```python
class Change(BaseModel):
    pk: Any
    record: dict[str, Any] | None = None


class RecordsChanged(Event):
    event_name = "records_changed"

    op: Literal["created", "updated", "deleted"]
    changes: list[Change]
```

One event per action, however many records it touched.

The topic is the routed base path: `request.scope["route"].path` with the action's relative path
(`cls.__viewset_metadata__["paths"][action]`) removed, computed in `__call__` and placed in the
context under `fastapi_viewsets.events.topic`, so that a `celery_viewset` action's worker, which has
no route, reads it from the context it was handed. An execution with neither, an action dispatched
outside any call, publishes to `cls.__viewset_metadata__["base_path"]`.

The pk of a returned record is read from the field named in
`cls.__viewset_metadata__["pk_field_name"]`, which `route_viewset` records from its argument;
without it, from the viewset's `pk_field_name` or `pk_field` attribute; with none of the three,
the first execution raises `TypeError` naming the viewset.

### Configuration

| Where | Value | Effect |
|---|---|---|
| `@action_configuration({ChangeEvents: False})` on a class or a `perform_*` method | | No event for that viewset or action |
| `@action_configuration({ChangeEvents: {"record": True}})` | `bool`, default `False` | `created` and `updated` changes carry `record`, the returned record as `model_dump(mode="json")` |
| `@action_configuration({ChangeEvents: {"topic": "..."}})` | `str` or `Callable[[ActionExecution], str]` | The topic to publish to, instead of the routed base path |
| `@action_configuration({ChangeEvents(): {...}})` | a fresh instance as the key | The plugin on this viewset only, with the global chain unchanged |

`record` carries the same bytes to every subscriber of the topic. A viewset whose `get_queryset`
narrows rows per caller keeps the default: the frontend retrieves the pk and a 404 ends the matter
for a row the caller may not see. `record: True` suits a viewset every subscriber reads in full, a
code table.

Subclass hooks, for what the configuration does not express:

```python
class ChangeEvents(Middleware):
    def topic_for(self, execution: ActionExecution) -> str: ...
    def changes_for(self, execution: ActionExecution, op: str, result: Any) -> list[Change]: ...
```

### Where it runs

`execute()` runs once per execution, where the action executes: in the FastAPI process for an
in-process action, in the Celery worker for a `celery_viewset` action. The worker therefore needs
`ChangeEvents` in its `settings.viewsets_command_middleware` and a `RedisEventStore` as its
`settings.viewsets_event_store`.

`ChangeEvents` publishes after `call_next()` returns. `DjangoORMViewSet` commits inside each
`perform_*`, so the change is committed by then. A middleware that opens a transaction in its own
`execute()` around the action is placed after `ChangeEvents` in the list, so that `ChangeEvents`
is the outer layer and publishes after that transaction has committed.

A viewset that writes outside the eight actions above, a custom endpoint, publishes itself:

```python
@__router.post("archive")
async def archive(self, context: Context, pk: int) -> None:
    await self.perform_archive(context, pk)
    await publish(await context["fastapi_viewsets.events.topic"], RecordsChanged(op="deleted", changes=[Change(pk=pk)]))
```

## Frontend

### The event client

```ts
import axios from 'axios';
import { connect } from 'muxws';
import { attachEvents, createEventClient, defineEvent } from '@dynamicforms/fastapi-viewsets';

const peer = await connect('wss://example.test/ws');
attachEvents(peer);                                                      // the peer's onStream handler
const events = createEventClient({ basePath: '/events', peer, axios });  // one per application

const ImportFinished = defineEvent<{ file: string; rows: number }>('import_finished');

const subscription = events.subscribe('imports:7', ImportFinished, {
  onEvent(payload) { notify(`${payload.file}: ${payload.rows} rows`); },
  onResync() { reloadImports(); },
});
await subscription.ready;
// ...
subscription.unsubscribe();
```

`createEventClient({ basePath, peer?, axios? })` builds a client. `peer` carries pushes and, while
open, the client's own calls; `axios` carries the calls while there is no open peer, polling
included. A client with neither throws. `eventsOf(peer)` returns the client registered for a peer,
and `createEventClient` registers the one it builds for its `peer`; a second client for the same
peer throws.

- `attachEvents(peer, fallback?)` installs an `onStream` handler that passes event streams to the
  peer's client and every other stream to `fallback`, or resets it with `REFUSED` without one. A
  muxws peer has one handler slot; a library or an application that owns it composes
  `handleEventStream(peer, payload, stream)` into its own handler instead. It returns `true` for a
  stream whose headers carry `:event`, having dispatched it, and `false`, having done nothing, for
  any other.
- `subscribe(topic, event, handlers)` returns an `EventSubscription`. The client holds one server
  subscription and one cursor per topic and counts the subscriptions on it. `ready` resolves once
  the topic is followed, over the socket or by the first poll, and rejects with
  `ViewSetRequestError` when the topic was refused, with the subscription removed. `unsubscribe()`
  of the last subscription on a topic sends `unsubscribe` over the socket, or stops polling it.
- An arriving event is dispatched by topic and event name to every subscription of that topic with
  that event name. An event of a topic the client does not follow is ignored.
- `onEvent` and `onResync` are required. A subscription that cannot say what it does on resync is
  one that silently stops matching the server after a gap.

The client's own calls, `subscribe`, `unsubscribe` and `poll`, are sent as one stream each over the
peer, with `:method` and `:path` headers and without `Idempotency-Key`, or as one axios request:
the calls are never repeated by the proxy machinery, because a repeat of `subscribe` answered by
`Idempotency` would not subscribe the new socket.

### Push, gap, poll and resync

Per topic the client holds `rev`, the last rev it applied, `null` until the first answer.

| Arrival | The event's `rev` and `prev` | Action |
|---|---|---|
| push or polled event | `prev` equals the held `rev` | `onEvent`, the held `rev` becomes the event's |
| push | `rev` above the held one, any other `prev` | one `poll` of that topic from the held `rev` |
| poll answer | `events` present | each event as above |
| poll answer | `events: null`, or the answer's `rev` below the held one | `onResync` on every subscription of the topic, the held `rev` becomes the answer's |
| any | `rev` at most the held one | ignored |

A tab the browser froze while the user was elsewhere comes back the same way: its next push or
poll names a `prev` it does not hold, the log of a minute ago is gone, and the tab reloads what it
shows. The log is not a backlog for an absent reader; it covers a gap of seconds.

While the peer is open the client sends `subscribe` once per topic and holds the ladder. Losing the
peer resumes the ladder on its base rung over `axios`; a client without `axios` waits for the
reconnect. On `peer.onReconnect` the client sends `subscribe` for every topic it holds; an answer
whose rev equals the client's resumes without a read, a larger one polls from the client's rev, and
once every topic has answered the ladder is held again. A subscribe refused on the new socket is
reported through `subscription.onError`, where given, and the subscription is removed.

The ladder is taskwire's, as a pure function in `vue/poll.ts`: 400 ms base, ×1.5 on a poll in
which no topic's rev moved, a 5 s ceiling, ±20 % jitter, and `poll_after_ms` as a floor the client
never polls below. Every topic the client follows goes into one `poll` call per rung.

### `watch()` on a ViewSet

```ts
class TrackViewSet extends muxwsViewSet<Track>()('id', [BulkViewSetMixin, PaginatedListMixin]) {}

const tracks = new TrackViewSet({ basePath: '/api/music', peer });
const rows = ref<Track[]>([]);

const watching = tracks.watch({
  fetch: true,
  onChange(change) { rows.value = applyChanges(rows.value, change, tracks.pkFieldName); },
  onResync() { void reload(); },
});
onUnmounted(() => watching.unsubscribe());
```

`watch(options)` subscribes the ViewSet's `basePath` to `records_changed` and returns the
subscription. It exists on every ViewSet: a muxws ViewSet uses `eventsOf(peer)`, a REST ViewSet the
client in `RestProxyOptions.events`, and either takes `events` in its options to use another
client. A REST ViewSet constructed without `events` throws from `watch()`.

| Option | Meaning |
|---|---|
| `onChange(change: RecordsChanged<T, K>)` | Required. Called once per event |
| `onResync()` | Required. Called on a resync of the topic |
| `fetch` | `true`: every `created` and `updated` change without `record` is completed with `retrieve(pk)` before `onChange`; a change whose retrieve answers 404 is dropped. Default `false` |
| `onError(error)` | A refused subscribe or poll of the topic |

`applyChanges(rows, change, pkFieldName)` returns a new array: `deleted` removes the rows with the
changed pks; `created` and `updated` replace the row with the same pk, or append the record when
there is none, and skip a change without `record`. A grid that holds the whole data set and sorts
and filters locally needs nothing else; one that pages on the server re-reads its page in
`onResync` and `onChange` alike.

## Changes to existing code

| Where | Change |
|---|---|
| `mux_ws.process_command` | `peer=` argument, placed in the request scope as `fastapi_viewsets.peer` |
| `decorators.route_viewset` | `__viewset_metadata__["pk_field_name"]` and `["paths"]` (action name to relative route path); `__viewset_class__` and `__viewset_action__` on every registered endpoint |
| `conf.Settings` | the eight settings above |
| `exceptions` | the four errors above, and their rows in the error code table |
| `vue/poll.ts` | the polling ladder, moved from taskwire |
| `vue/events.ts` | the event client |
| `vue/proxy-base.ts`, `rest-proxy.ts`, `muxws-proxy.ts` | `watch()`, the `events` option |
| `vue/index.ts` | `attachEvents`, `handleEventStream`, `createEventClient`, `eventsOf`, `defineEvent`, `applyChanges`, the ladder, the `RecordsChanged`, `EventClient` and `EventSubscription` types |
| `pyproject.toml` | muxws floor `0.4.1`; no new extra: `RedisEventStore` takes a client the application constructs, as `Idempotency` does |
| `package.json` | muxws peer floor `^0.4.1` |
| docs | a guide page and an API page for events, the `ChangeEvents` row in the built-in middleware table, the muxws page's server example with `peer=` |

`fastapi_viewsets.events` imports muxws nowhere: it calls `peer.notify` and `peer.on_close` on the
object the scope carries. `RedisEventStore` lives in `fastapi_viewsets.events.redis` and is the
only module of the package that imports `redis`.

## Not in this version

- **Re-authorization of a held subscription.** A socket's subscription is authorized when it is
  made; a poll is authorized on every call. An application whose callers lose access while
  connected closes their socket, `peer.close(reason=...)`, and the frontend subscribes again under
  its current identity.
- **Topic patterns.** Matching is exact. An application that needs "every club" publishes to a
  topic for that.
- **A shared `onStream` on the client.** `handleEventStream` is what another library composes into
  the handler it installs; a library that installs its own handler without a composition point
  cannot share a peer with the event client.

## Design decisions

- **Change events come from `Middleware.execute()`, not from hooks in each backend.** `execute()`
  runs exactly once per execution, in whichever process the action executes, with the viewset,
  the action, its arguments and its result. It covers every `ImplMixin` backend and every
  application override of a `perform_*` method without a line in any of them, and it is switched
  per viewset and per action by `@action_configuration`, the mechanism every other middleware is
  configured by.
- **The store is the truth and the push is an accelerator.** A push that is dropped, a socket that
  is lost and a process that holds no socket all end in the same place: the next poll answers from
  the store, and the worst case is one resync. There are no delivery guarantees to engineer on the
  socket.
- **A per-topic rev with a `prev` link instead of a per-document rev.** A viewset's data has no
  single document to carry a rev; the topic carries it. A gap is one comparison of `prev` against
  the held rev, which stays correct when the counter expires and restarts, and a bounded log turns a
  gap of seconds into a poll rather than a re-read. `log_size = 0` is taskwire's model exactly.
- **A short log, a permanent counter.** A reader absent for a minute re-reads its state; a backlog
  for it would be stale data delivered late. The counter outlives the log so that a reader that is
  present applies a sparse topic's next event as a delta, without the reload that a restarted
  counter would force on it. A keyed topic that is finished is dropped by its source.
- **Subscribe and poll are viewset commands.** They pass through the application's command
  middleware, context processors, rate limiting and `/schema` like any other call. The only
  transport-specific piece is the socket in the request scope.
- **One push per event, no long-lived stream.** `process_command` serves a command as one ASGI
  request with one buffered response, and a subscription held open as a stream would need a
  second dispatch path beside it. `notify()` leaves nothing open on either side.
- **Two topic syntaxes.** A path is a viewset, a name is a declared source, and the two cannot
  name the same thing. A name is declared once per process, so two modules cannot publish under one
  name without one of them failing at import.
- **`record` is opt-in.** A payload reaches every subscriber of a topic with the same bytes, and
  `ViewsetTopics` admits a caller the viewset's `list_items` admits; row-level narrowing in
  `get_queryset` is invisible to both. A pk is safe for every subscriber; a record is safe only
  where every subscriber may read every row.
- **No authorizer, no subscription.** A topic is a string any connected client can name. Admission
  is declared, `allow_all` included, never assumed.
- **The general layer lives here, not in taskwire.** taskwire depends on this package in both
  languages (`taskwire[viewsets]`, `taskwire[muxws]`, `taskwire/muxws` on npm), so the shared
  mechanism can only sit below it. taskwire's polling ladder moves here and taskwire imports it;
  taskwire's push path may later publish its envelopes through this store, with a delivery filter
  carrying its per-connection decisions, which is what `viewsets_event_delivery_filters` is for.

## Implementation plan

Three pull requests here, then one in taskwire. Each lands with its tests, its documentation pages
and its changelog entry; the version becomes 0.10.0 with the first.

### PR 1: the backend

1. `fastapi_viewsets/events/__init__.py`: `Event`, `publish`, `source`, `Source`, the topic
   validation, `Subscription`, the subscription table, `deliver`, `start_events`, `stop_events`,
   `check_events`.
2. `events/store.py`: `EventStore`, `LoggedEvent`, `MemoryEventStore`. `events/redis.py`:
   `RedisEventStore` and its reader, after `celery_viewset/result_reader.py`.
3. `events/authorizers.py`: `EventAuthorizer`, `ViewsetTopics`, `DeclaredSources`, `allow_all`,
   `allow_authenticated`. `events/viewset.py`: `EventsViewSet`, `register_events`.
4. `events/changes.py`: `Change`, `RecordsChanged`, `ChangeEvents`.
5. `conf.Settings`, `exceptions`, `mux_ws.process_command(peer=)`, `route_viewset` metadata and
   endpoint markers.
6. Tests: the store contract run against both stores (Redis skipped when absent, as
   `idempotency_test.py` does), the viewset over both transports through the application,
   `ViewsetTopics` against `Session` and `Authorization`, `ChangeEvents` for every action and for a
   `celery_viewset` action in the worker, a mounted router prefix, the delivery filter, the
   subscription limit, socket loss.
7. Docs: `docs/guide/events.md`, `docs/api/events.md`, the rows in `command-middleware.md`,
   `error-codes.md`, `settings.md`, the `peer=` line in `muxws.md`, the sidebar.

### PR 2: the frontend

1. `vue/poll.ts`: `PollSettings`, `DEFAULT_POLL_SETTINGS`, `PollLadderState`, `ladderInterval`,
   `jitterMultiplier`, `nextDelay`, `PollLadder`, with taskwire's tests for them.
2. `vue/events.ts`: `EventClient`, `createEventClient`, `eventsOf`, `attachEvents`,
   `handleEventStream`, `defineEvent`, `EventSubscription`, the cursor table, the push/gap/poll/
   resync rules, the reconnect sequence.
3. `watch()` and the `events` option on both proxies; `applyChanges`; the exports.
4. Tests: the rules of "Push, gap, poll and resync" one by one against a fake peer and a fake
   axios, the reconnect sequence, the real-muxws spec extended with one subscribe and one push,
   `applyChanges`.
5. Docs: the Vue sections of `events.md`, `vue-mixins.md` and `viewset-factory.md` for `watch()`.

### PR 3: the demo

1. `demo/backend`: `register_events`, `ChangeEvents` on both music viewsets, `ViewsetTopics`,
   `MemoryEventStore`, `peer=` on the socket endpoint.
2. `demo/frontend`: the grid follows its viewset with `watch({ fetch: true })`; a second tab's edit
   appears in the first.
3. `demo/e2e`: two pages, one edit, one assertion on the other page, over REST and over muxws.

### taskwire

One pull request, after PR 2 is published:

1. `ts/poll.ts` imports the ladder from `@dynamicforms/fastapi-viewsets` and re-exports it; its
   tests stay as consumer tests of the same rules (TW-REST-010, -011, -014 unchanged).
2. `MuxwsClientTransport` installs an `onStream` that calls `handleEventStream(peer, payload,
   stream)` first and delivers its own envelope otherwise, so one peer serves both libraries.
3. Peer floors: `@dynamicforms/fastapi-viewsets ^0.10.0`, `muxws ^0.4.1`.
4. A `GAPS.md` entry for the push path: `MuxwsTransport` and `RedisReader` against
   `publish("taskwire:" + ns, envelope)` with a delivery filter carrying TW-DEL and TW-PRIV, which
   touches TW-TR-004, TW-BP-001 and TW-BP-002 and is a spec change to decide separately.
