# Events

A viewset answers when the frontend asks. `fastapi_viewsets.events` carries the other direction: the
backend publishes an event to a topic, and every frontend following that topic learns of it, as a
push over its [muxws](./muxws) socket or on its next poll.

The package supplies:

- the publish side: `Event` classes, sources of custom topics, `publish()`, and an event store;
- the subscribe side: a viewset with `subscribe`, `unsubscribe` and `poll`, a list of authorizers,
  a list of delivery filters, and the delivery of each event to the subscribed sockets;
- `ChangeEvents`, a [command middleware](./command-middleware) that publishes `RecordsChanged` after
  every create, update and destroy action of a viewset.

Which events exist, which topics carry them and what a frontend does with them is the application's.

The API reference is [events](../api/events).

---

## Delivery model

Every event is recorded in the store before it is pushed anywhere, and `poll` answers from the
store.

- A frontend holding a muxws socket subscribes to a topic and receives each event as a push.
- A frontend without a socket, or whose socket was lost, polls the store.
- A push that fails or times out is dropped. The frontend learns of the event on its next poll.

A deployment without muxws is complete; its frontends poll.

The store assigns every event of a topic two integers:

| Field | Value |
|-------|-------|
| `rev` | `max(the topic's previous rev + 1, now in milliseconds)`. Strictly increasing per topic, and larger than every rev issued before the topic's counter was lost or dropped |
| `prev` | The topic's rev before the event; `0` for a topic without one |

Each event also carries its [`event_version`](#event-versions), the version of its payload's shape.

A push and a poll answer carry the same `rev` and version for an event. A push carries the event's
`prev`; a poll answer chains the events it returns, so an event's `prev` there is the rev of the
event before it in the answer, the cursor for the first, which differs from the event's own `prev`
only when the [delivery filters](#delivery-filters) left events out. A client that holds
the rev `r` of a topic applies an event whose `prev` is `r`. An event with a larger `rev` and another
`prev` means the client missed events; one `poll` from `r` returns them, or `events: null` when the log no
longer holds them, in which case the client re-reads the state it displays. An event whose `rev` is
at most `r` is already applied.

Two events published in the same millisecond receive consecutive revs. Two processes publishing at
once receive distinct, ordered revs, because `append` is atomic; their pushes may still reach a
socket out of order.

A rev is below `2**53` (`MAX_REV`), so it is exact as a JavaScript number, and travels as a JSON
number on both transports. A store raises `OverflowError` instead of issuing `MAX_REV`.

---

## Topics

A topic is a string. Matching is exact. There are two forms, which cannot name the same topic:

| Form | Syntax | Declared by | Example |
|------|--------|-------------|---------|
| viewset | `/` followed by the path, the routed base path of a viewset | `route_viewset`, with the `include_router` prefixes | `/api/music` |
| custom | `name` or `name:key`; `name` matches `[a-z0-9_.-]+`, `key` is non-empty | `source(name)` | `imports`, `imports:7` |

Neither form contains whitespace or a control character. A key may contain `:`; `a:b:c` is the
source `a` with the key `b:c`.

A viewset's topic is the path its routes are routed under, `include_router` prefixes included,
which is the `basePath` a frontend ViewSet uses. A path parameter of the prefix or of `base_path`
has its value in the topic: a viewset at `/clubs/{club_id}/tracks` has the topic `/clubs/7/tracks`
for club 7, a topic per club. A viewset mounted on two routers has two topics. A Starlette `Mount`
path in front of the application is not part of the topic.

### Custom topics: `source()`

```python
from fastapi_viewsets.events import Event, allow_authenticated, source


class ImportFinished(Event):
    event_name = "import_finished"

    file: str
    rows: int


imports = source("imports", authorizer=allow_authenticated)

await imports.publish(ImportFinished(file="members.csv", rows=412))            # topic "imports"
await imports.publish(ImportFinished(file="members.csv", rows=412), key="7")   # topic "imports:7"
await imports.drop(7)                                                          # forgets "imports:7"
```

`source(name, authorizer=None)` declares `name` in a registry of the process and returns a `Source`.
A name that does not match `[a-z0-9_.-]+`, or that is already declared, raises `ValueError`, so two
modules cannot publish under one name. `authorizer` is consulted by
[`DeclaredSources()`](#declaredsources) for the source's topics.

| Method | Effect |
|--------|--------|
| `topic(key=None)` | `name:key`, or `name` without a key; `ValueError` when the result is not a topic |
| `async publish(event, key=None)` | `publish(self.topic(key), event)` |
| `async drop(key)` | Removes the rev and the log of `name:key` from the store. The topic's next event, if any, carries `prev` `0` |

`event_name` is unique within a topic's events, not globally: a frontend dispatches on the pair
(topic, event name). `records_changed` is the event `ChangeEvents` publishes on viewset topics.

---

## Events and `publish()`

`Event` is a pydantic `BaseModel` with a class-level `event_name: ClassVar[str]` and
`event_version: ClassVar[int] = 1`. A subclass without a non-empty string `event_name`, or with an
`event_version` that is not an `int` of at least `1` (a `bool` is not), raises `TypeError` when the
class is created. Both are class attributes, set by plain assignment or as a `ClassVar`: a subclass
that annotates either one as a field (`event_version: int = 2`) makes it part of the payload, and
raises `TypeError` when the class is created. The payload is `event.model_dump(mode="json")`.

### Event versions

`event_version` is the version of the payload's shape. Every push and every poll entry carries it
next to the event name, so a consumer tells the shapes of one event's payload apart and handles
several versions side by side: it dispatches on the event name and the version, and reads a topic,
or a stored series of its events, in which payloads of several versions occur.

```python
class ImportFinished(Event):
    event_name = "import_finished"
    event_version = 2

    file: str
    rows: int
    skipped: int
```

An event that does not set `event_version` has version `1`; a subclass inherits its parent's.
`RecordsChanged` is version `1`.

### `publish()`

`await publish(topic, event)`:

1. raises `ValueError` for a topic of neither form, and `TypeError` for an `event` that is not an
   `Event`; the topic's form is all that is checked;
2. `rev, prev = await store.append(topic, event.event_name, payload, event.event_version)`;
3. `await store.publish(topic, rev, prev, event.event_name, payload, event.event_version)`, which
   reaches `deliver()` in every process that watches the topic. An exception of this step is logged
   at warning level and not raised: the event is recorded and is answered by the next poll;
4. returns `(rev, prev)`.

`publish()` publishes at once. An event that describes a database change is published after the
transaction commits; published earlier, a frontend reacting to it reads the state before the change.
It may be called from any process with a store: a request handler, a Celery worker, a management
command.

---

## The store

`settings.viewsets_event_store` holds the store. `None`, the default, is one `MemoryEventStore` per
process, created on first use.

| | `MemoryEventStore()` | `RedisEventStore(redis_client, key_prefix="events:")` |
|-|----------------------|--------------------------------------------------------|
| Reach | the process | every process that reaches the Redis |
| `append` | one synchronous step without an `await` | one Lua script |
| `publish` | `deliver()` in the same process | `PUBLISH` on `{prefix}ch:{topic}`; the reader of each watching process calls `deliver()` |
| `watch` / `unwatch` | no-op | `SUBSCRIBE` / `UNSUBSCRIBE` on the reader's connection |
| `start` / `stop` | no-op | opens / closes the reader |

`redis_client` is a `redis.asyncio.Redis` the application constructs. `RedisEventStore` lives in
`fastapi_viewsets.events.redis`, the only module of the package that uses `redis`. Its keys:

| Key | Content |
|-----|---------|
| `{prefix}rev:{topic}` | The topic's rev, a decimal string without expiry |
| `{prefix}log:{topic}` | The topic's log, a list of JSON entries, newest first, at most `viewsets_event_log_size` long, expiring `viewsets_event_log_ttl` seconds after the topic's last event |
| `{prefix}ch:{topic}` | The pub/sub channel of the topic's events |

### The log and the counter

A topic's log holds its last `settings.viewsets_event_log_size` events (default `32`) and expires
`settings.viewsets_event_log_ttl` seconds (default `60.0`) after the topic's last event.
`MemoryEventStore` empties an expired log when it next reads or appends to it. With a log size of
`0` the log is empty, and a poll from any cursor behind the topic's rev answers `events: null`.

The counter has no expiry. On a topic with sparse events, a frontend holding the last rev receives
the next event with `prev` equal to it. A counter that is lost (a `MemoryEventStore` whose process
restarted, a flushed Redis, a dropped topic) restarts at the current time in milliseconds, which is
larger than every rev issued before, and the next event carries `prev` `0`. No frontend holds `0`;
each one polls, finds no event with its `prev`, and re-reads its state.

`store.since(topic, after)` answers a poll:

| Case | Answer |
|------|--------|
| `after` is the topic's rev | `[]` |
| the log holds the event whose `prev` is `after` | that event and every newer one, oldest first |
| otherwise, a cursor ahead of the topic's rev included | `None` |

### Lifespan

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from fastapi_viewsets.conf import settings
from fastapi_viewsets.events import check_events, start_events, stop_events
from fastapi_viewsets.events.redis import RedisEventStore

settings.viewsets_event_store = RedisEventStore(Redis.from_url("redis://localhost"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_events()
    check_events()
    yield
    await stop_events()
```

`start_events()` calls `start()` of the configured store, `stop_events()` its `stop()`.
`check_events()` logs a warning naming `await start_events()`, and returns `False`, when the store
has a reader and the reader does not run; otherwise it returns `True`.

The `RedisEventStore` reader is one task on one pub/sub connection. It subscribes to the channel of
every topic the process watches; a topic watched before `start()` is subscribed when the reader
opens. A failed read is logged and retried after one second; a message that cannot be delivered is
logged and skipped.

### Processes

A process watches a topic while it holds at least one subscription for it. An event published in a
process that holds no socket, a Celery worker, reaches the sockets through Redis only. With a
`MemoryEventStore` in a worker, `publish()` records the event in the worker's store and delivers it
to nobody, and a frontend polling a web process reads that process's store. A deployment of more
than one process configures a `RedisEventStore` in every process, workers included.

---

## Subscribing and polling: `register_events`

The subscribe surface is a viewset, so the application's command middleware, context processors and
`/schema` apply to it:

```python
from fastapi import APIRouter

from fastapi_viewsets.events import register_events

router = APIRouter()
events_viewset = register_events(router, base_path="/events")
app.include_router(router, prefix="/api")
```

`register_events(router, base_path="/events")` decorates a new subclass of `EventsViewSet` with
`route_viewset(router, base_path)` and returns it. Each call mounts a viewset of its own, so the
viewset can be mounted on more than one router, and the returned class takes `@action_configuration`
like any viewset:

```python
from fastapi_viewsets.action_configuration import action_configuration
from fastapi_viewsets.middleware.auth import Session

action_configuration({Session: False})(events_viewset)
```

| Call | Transport | Request body | Answer |
|------|-----------|--------------|--------|
| `POST {base_path}/subscribe` | muxws | `{"topic": "/api/music"}` | 200 `{"rev": 17}`, the topic's current rev |
| `POST {base_path}/unsubscribe` | muxws | `{"topic": "/api/music"}` | 204, also for a topic the socket does not hold |
| `POST {base_path}/poll` | REST and muxws | `{"cursors": {"/api/music": 17, "imports:7": null}}` | 200, [below](#poll) |

The transports are set with `@transports` on each endpoint and do not depend on
`settings.viewsets_register_muxws`.

### `peer=`

A subscription names a socket. `subscribe` and `unsubscribe` read it from
`request.scope["fastapi_viewsets.peer"]`, which `process_command` sets from its `peer` argument:

```python
@app.websocket("/ws")
async def muxws_endpoint(websocket: WebSocket) -> None:
    peer = await accept(websocket)
    peer.on_stream(lambda payload, stream: process_command(payload, stream, connection=websocket, peer=peer))
    await peer.serve()
```

`subscribe` or `unsubscribe` served without `peer=` raises `RuntimeError` naming
`process_command(..., peer=peer)`; the caller receives a 500, and the log carries the message.

### `subscribe` and `unsubscribe`

`subscribe`:

1. reads the socket; without one, `RuntimeError`;
2. a topic of neither form is 422 `event_topic_invalid`;
3. runs the [authorizers](#authorization); a refusal is 403 `event_topic_refused`, or the status of
   the `HTTPException` an authorizer raised;
4. subscribes the socket and waits until the store watches the topic; a socket that already holds
   `settings.viewsets_event_max_subscriptions` other topics (default `256`) is 409
   `event_subscriptions_exceeded`;
5. answers the topic's current rev, read after the store watches the topic, so every event
   published after the rev the answer carries is pushed to the socket.

A topic the socket already holds is subscribed again with the new call's context and counts once.

Subscriptions are held per process, in a table keyed by topic and socket. The first subscription of
a topic in the process calls `store.watch(topic)`; when that raises, the subscription is removed and
the call fails. A `subscribe` of a topic whose `watch` or `unwatch` is running waits for it to end
and then calls `store.watch(topic)` itself when the topic is still not watched, so each `subscribe`
answers once the topic is watched or fails with the error of its own `watch`. `RedisEventStore`'s
`watch` returns once Redis has confirmed the `SUBSCRIBE`. The last subscription removed calls
`store.unwatch(topic)`; an exception it raises is logged.

The first subscription of a socket installs one `peer.on_close` handler, which drops every
subscription of the socket. A command runs to completion after its socket is lost; a `subscribe`
that reaches the table after its socket closed (`peer.is_open` false) is not entered in it and does
not watch the topic. A reconnected peer is a new socket and subscribes again.

`unsubscribe` removes the socket's subscription to the topic and answers 204. It neither validates
nor authorizes the topic.

### `poll`

```json
{
  "topics": {
    "/api/music": {
      "rev": 19,
      "events": [
        {"rev": 18, "prev": 17, "event": "records_changed", "version": 1, "payload": {"op": "updated", "changes": [{"pk": 3}]}},
        {"rev": 19, "prev": 18, "event": "records_changed", "version": 1, "payload": {"op": "deleted", "changes": [{"pk": 4}]}}
      ]
    },
    "imports:7": {"rev": 3, "events": []},
    "club:9": {"rev": null, "events": null, "error": "event_topic_refused"}
  },
  "poll_after_ms": 0
}
```

A poll carrying more than `settings.viewsets_event_max_subscriptions` cursors is 422
`event_cursors_exceeded`. Otherwise every cursor is answered, in the request's order:

| Topic and cursor | `rev` | `events` | `error` |
|------------------|-------|----------|---------|
| a topic of neither form | `null` | `null` | `event_topic_invalid` |
| refused by the authorizers | `null` | `null` | `event_topic_refused`, or the `code` of the `DfViewSetError` an authorizer raised (`session_expired`, `not_authorized`, `rate_limited`, ...); `event_topic_refused` for an `HTTPException` without a `code` |
| cursor `null` | the current rev | `[]` | absent |
| cursor equal to the current rev | the current rev | `[]` | absent |
| any other cursor | the current rev | `store.since(topic, cursor)`: the events after the cursor that the [delivery filters](#delivery-filters) pass, or `null` when the log cannot answer it | absent |

A refused or invalid topic does not affect the other topics. The authorizers run for every topic of
every poll. In `events`, each event's `prev` is the rev of the event before it in the answer, the
cursor for the first. `rev` is never lower than the rev of any event after the cursor, also when an
event is appended while the poll is answered, and covers the events the delivery filters left out.
`poll_after_ms` is `settings.viewsets_event_poll_after_ms` (default `0`): the shortest interval, in
milliseconds, at which a client polls.

The events viewset goes through `settings.viewsets_command_middleware` like any viewset. With
`Idempotency` in the chain, a `subscribe` repeated with the same `Idempotency-Key` is answered from
the stored outcome and does not subscribe the socket it arrives on.

---

## Authorization

```python
from fastapi_viewsets.conf import settings
from fastapi_viewsets.events import DeclaredSources, ViewsetTopics

settings.viewsets_event_authorizers = [ViewsetTopics(), DeclaredSources()]
```

An authorizer is `EventAuthorizer`, `await authorizer(request, context, topic) -> bool | None`. The
authorizers run in list order with the calling request, its context and the topic, on every
`subscribe` and for every topic of every `poll`:

- `True` admits the topic; the authorizers after it do not run.
- `False` refuses it.
- `None` leaves the decision to the next one.
- No decision refuses the topic. The default list is empty and refuses every topic.
- An exception propagates and the authorizers after it do not run. An `HTTPException`, FastAPI's or
  Starlette's, refuses the topic with that exception: its status on `subscribe`, its `code` in the
  topic's `error` on `poll`. Any other exception is a 500.

An authorizer may also return its decision without an awaitable.

A held subscription is not authorized again. An application whose callers lose access while
connected closes their socket (`peer.close()`), and the frontend subscribes again under its current
identity.

### `ViewsetTopics`

`ViewsetTopics()` admits a viewset's topic to a caller the viewset itself admits:

1. A topic that does not start with `/` gets `None`.
2. The viewset is looked up on the routes of `request.app`, `include_router` prefixes included, in
   routing order: the first route whose endpoint carries `__viewset_class__` and, as
   `__viewset_action__`, the class's `list_items`, or `retrieve` for a class without `list_items`,
   and whose path without that action's path in `__viewset_metadata__["paths"]` matches the topic as
   Starlette matches a request path. A path parameter matches one segment of the topic and is
   converted by its convertor: `/clubs/{club_id:int}/tracks` matches `/clubs/7/tracks` with
   `club_id` `7`. No such route gets `None`. A viewset in an application mounted with Starlette's
   `Mount` under `request.app` is not found.
3. The checks below receive the calling request with the topic's path parameters as
   `request.path_params`.
4. The route's `dependencies` run as FastAPI runs them before the endpoint: those of
   `FastAPI(dependencies=...)`, of `include_router(..., dependencies=...)`, of
   `APIRouter(dependencies=...)` and of the route itself, with `app.dependency_overrides`. A
   parameter of one that does not validate refuses the topic (`False`). The endpoint's own
   parameters are not resolved.
5. The action's `@action_configuration` is resolved, and the calling context is rebound to it,
   `Context(context.raw(), action_configuration=..., action_name=...)`; the calling context is left
   unchanged.
6. `depends()` of every effective `Middleware` runs with the viewset class and the rebound context:
   `settings.viewsets_command_middleware`, then the instances the configuration injects. An
   `HTTPException` propagates: [`Session`](./authentication#rejecting-unauthenticated-requests-session)
   raises 401, [`Authorization`](./authorization) 403, [`RateLimiter`](./rate-limiter) 429 and counts
   the call as one call of that action.
7. When none raises, the topic is admitted.

An `Authorization` check reads the path parameters from the request it receives:

```python
async def club_member(request: Request, cls: type, context: Context) -> bool:
    return await is_member(await context.user, request.path_params["club_id"])
```

Row-level narrowing in a viewset's `get_queryset` is not part of this decision.

### `DeclaredSources`

`DeclaredSources()` answers a custom topic with the answer of the `authorizer` of the source that
declared its name. A viewset topic, a name no source declared, and a source without an authorizer get
`None`.

### `allow_all`, `allow_authenticated`

`allow_all` admits every topic. `allow_authenticated` admits a topic when `await context.user` is
truthy, and answers `None` otherwise and for a context without `user`, so a later authorizer can
still decide. Both are plain functions and are listed without calling them:

```python
settings.viewsets_event_authorizers = [ViewsetTopics(), allow_authenticated]
```

---

## Delivery

`deliver(topic, rev, prev, name, payload, version)` is the entry point into a process's
subscriptions; both stores call it. For each subscription of the topic in the process it starts one
task and returns without waiting for them, so a slow delivery filter for one socket does not delay
the others. A
task:

1. runs the [delivery filters](#delivery-filters); a `False` skips the socket;
2. sends one push, a one-shot stream (one `open` frame with `end` set), which is not answered:

   ```python
   await peer.notify(
       payload,
       headers={":event": name, ":version": version, ":topic": topic, ":rev": rev, ":prev": prev},
   )
   ```

Both steps together are bounded by `settings.viewsets_event_push_timeout` seconds (default `2.0`).
`notify()` queues the push on the socket and returns without waiting for the socket to send it, so
the bound applies to the delivery filters and the queueing; a socket that does not read does not
make a push time out. A push that raises muxws's `ConnectionLost`, which `notify()` raises on a
closed socket, drops every subscription of its socket. Any other failure, the timeout included, is
logged at debug level and the push is dropped. `publish()` never raises for a delivery failure.

`fastapi_viewsets.events` does not import muxws. It calls `notify()` and `on_close()` on the peer the
request scope carries and recognises `ConnectionLost` by its class name.

### Delivery filters

```python
from typing import Any

from fastapi_viewsets.events import Subscription


def skip_empty_imports(
    subscription: Subscription, topic: str, rev: int, name: str, payload: Any, version: int
) -> bool:
    return name != "import_finished" or payload["rows"] > 0


settings.viewsets_event_delivery_filters = [skip_empty_imports]
```

`DeliveryFilter` is `filter(subscription, topic, rev, event_name, payload, version) -> bool`, sync
or async; `version` is the event's `event_version` and comes last. The filters run in list order
for every push and for every event a `poll` answers after the cursor:

- a result that `is False` skips the socket, or leaves the event out of the poll answer, and the
  filters after it do not run;
- any other result, `None` included, passes to the next filter;
- a filter that raises counts as `True` and is logged at debug level.

`subscription` carries `peer`, `context` and `topic`:

| Call | `peer` | `context` |
|------|--------|-----------|
| push | the socket of the subscription | the context of the `subscribe` call |
| `poll` over muxws | the socket the poll arrived on | the context of the `poll` call |
| `poll` over REST | `None` | the context of the `poll` call |

A filter runs after the authorizers have admitted the topic. The name and the payload of an event a
filter refuses reach neither the socket nor the poll answer. Its rev does: a later push carries it
as `:prev`, and a poll answer's `rev` covers it. A filter that answers from `context` decides on the
context of the call it runs for, so a push and a poll decide alike when their contexts carry the
same identity. The list is empty by default.

---

## `ChangeEvents`

```python
from fastapi_viewsets.conf import settings
from fastapi_viewsets.events import ChangeEvents, ViewsetTopics
from fastapi_viewsets.middleware.auth import Session
from fastapi_viewsets.middleware.idempotency import Idempotency

settings.viewsets_command_middleware = [Idempotency(...), ChangeEvents(), Session()]
settings.viewsets_event_authorizers = [ViewsetTopics()]
```

`ChangeEvents()` is a command middleware that publishes one `RecordsChanged` to a viewset's topic
after each of its write actions:

| `execution.action` | `op` | `changes` |
|--------------------|------|-----------|
| `create` | `created` | the pk of the returned record |
| `bulk_create` | `created` | the pk of each returned record |
| `update`, `partial_update` | `updated` | the pk of the returned record |
| `bulk_update`, `bulk_partial_update` | `updated` | the pk of each returned record |
| `destroy` | `deleted` | the `pk` argument |
| `bulk_destroy` | `deleted` | each pk of the `pk` argument |

```python
class Change(BaseModel):
    pk: Any


class RecordsChanged(Event):
    event_name = "records_changed"

    op: Literal["created", "updated", "deleted"]
    changes: list[Change]
```

One event per action, however many records it touched. Any other action publishes nothing; an
action that raises publishes nothing. A `ViewSetResult` returned by the action contributes its
`body`.

A change carries the pk only. A frontend reads a changed record through the viewset's own
`retrieve`, which answers in the endpoint's format (its `response_model`, aliases and command
middleware applied) and in the reader's context: a `get_queryset` that narrows rows per caller
answers 404 for a row the reader may not read. The record an action returns has neither: it is
built in the writer's context, before the response is serialized.

The pk of a returned record, a model or a dict, is read from the field named by
`__viewset_metadata__["pk_field_name"]` (the `pk_field_name` argument of `route_viewset`); without
it, by the viewset's `pk_field_name` attribute, else its `pk_field` attribute. A `CollectionViewSet`'s
`pk_field_name` is its `pk_field`; a `DjangoORMViewSet`'s is the model's primary key. A viewset with none of
the three raises `TypeError` naming the viewset, before a `created` or `updated` action runs. A record
without that field raises `TypeError` after the action has run. The pk of a destroy is the `pk`
argument, passed by keyword or by position, and needs no pk field.

### Topic

`ChangeEvents.__call__` runs in the web process on every action of the viewsets it applies to. It
stores the routed base path of the call's viewset in the context under
`"fastapi_viewsets.events.topic"` (`TOPIC_CONTEXT_KEY`). `routed_base_path(request)` computes it:
the request's path, relative to the application's `root_path`, with as many trailing segments
removed as the action's path in `__viewset_metadata__["paths"]` has. Each path parameter of that
path matches one segment; a `{name:path}` parameter is not supported. Path parameters of the prefix
and of `base_path` keep their values: `/clubs/7/tracks` for a request to `/clubs/7/tracks/1`.

`execute()` publishes to, in order:

1. the configured `topic` (below);
2. for an execution a request called (`execution.parent` is `None`), the topic in the context,
   which a `celery_viewset` worker receives with the context it is handed;
3. `__viewset_metadata__["base_path"]`, for an execution dispatched outside any call, and for one
   dispatched in the execution of another action: a `celery_viewset` action called from another
   action's code receives that action's context, whose topic belongs to the other viewset;
4. none of the three raises `TypeError`.

A `celery_viewset` worker class has no `__viewset_metadata__`. A `celery_viewset` action that
another action calls therefore needs a configured `topic`; without one, `execute()` raises
`TypeError` in the worker before the action runs.

The topic is validated before the action runs; an invalid topic raises `ValueError` and the action
does not run.

### Configuration

[`@action_configuration`](./action-configuration) on a viewset class or on a `perform_*` method:

| Key | Value | Effect |
|-----|-------|--------|
| `ChangeEvents` | `False` | No event for that viewset or action |
| `ChangeEvents` | `True` or absent | The defaults |
| `ChangeEvents` | `{"topic": "..."}` | A `str`, or a `Callable[[ActionExecution], str]`, the topic to publish to instead of the routed base path |
| `ChangeEvents()` | `None`, `True` or a dict as above | Injects an instance for that viewset or action only; the instance reads the value under its own key, else under its class |

Any other value, or a dict with a key other than `topic`, raises `TypeError`. A class
key requires a `ChangeEvents` instance in `settings.viewsets_command_middleware`; without one it
raises `ValueError` at request time (see
[Configuring an existing middleware](./action-configuration#configuring-an-existing-middleware)).
An injected instance replaces the `ChangeEvents` of the global list for its viewset or action: while
the action configuration has a `ChangeEvents` instance as a key, a `ChangeEvents` that is not one
of its keys publishes nothing, so the action publishes one event.

### Subclass hooks

```python
class ChangeEvents(Middleware):
    def topic_for(self, execution: ActionExecution) -> str: ...
    def changes_for(self, execution: ActionExecution, op: str, result: Any) -> list[Change]: ...
```

`topic_for` returns the topic per the order above. `changes_for` returns the changes of one
execution per the table above.

### Where it runs

| Part | In-process action | `celery_viewset` action |
|------|-------------------|-------------------------|
| `__call__`, which stores the topic | FastAPI process | FastAPI process |
| `execute()`, which publishes | FastAPI process | Celery worker |

The worker therefore needs `ChangeEvents` in its `settings.viewsets_command_middleware` and a
`RedisEventStore` as its `settings.viewsets_event_store`.

`execute()` publishes after `call_next()` returns. `DjangoORMViewSet` commits inside each
`perform_*`, so the change is committed by then. A middleware that opens a transaction in its own
`execute()` around the action is listed after `ChangeEvents`, so that `ChangeEvents` is the outer
layer and publishes after the commit. A call that `Idempotency` answers from a stored outcome does
not reach `execute()` and publishes nothing.

An exception of the publish propagates from `execute()` after the action has run: the change is
committed and the caller receives the error. `store.append` failing, a Redis that cannot be reached
included, is one: it assigns no rev, so the topic shows no gap and no frontend learns of the change
from the store. With `Idempotency` in the chain, a repeat of the call with the same
`Idempotency-Key` is answered with the stored error and does not write again.

### Custom endpoints

An endpoint that writes outside the eight actions publishes itself, to the topic `__call__` stored:

```python
from fastapi_viewsets.events import TOPIC_CONTEXT_KEY, Change, RecordsChanged, publish


@__router.post("/{pk}/archive")
async def archive(self, context: Context, pk: int) -> None:
    await self.perform_archive(context, pk)
    await publish(await context[TOPIC_CONTEXT_KEY], RecordsChanged(op="deleted", changes=[Change(pk=pk)]))
```

---

## Settings

| Setting | Default | Meaning |
|---------|---------|---------|
| `viewsets_event_store` | `None` | The `EventStore`; `None` is one `MemoryEventStore` per process |
| `viewsets_event_authorizers` | `[]` | The authorizers, in order; an empty list refuses every topic |
| `viewsets_event_delivery_filters` | `[]` | The delivery filters, in order |
| `viewsets_event_log_size` | `32` | Events kept per topic for `poll` |
| `viewsets_event_log_ttl` | `60.0` | Seconds a topic's log is kept after its last event; the rev is kept without expiry |
| `viewsets_event_push_timeout` | `2.0` | Seconds the delivery filters and `peer.notify()` of one push to one socket may take together |
| `viewsets_event_max_subscriptions` | `256` | Topics one socket may hold, and cursors one poll may carry |
| `viewsets_event_poll_after_ms` | `0` | `poll_after_ms` of every poll answer, in milliseconds |

Every one is read on use. See [settings](../api/settings).

## Errors

| Code | Status | Class | Raised when |
|------|--------|-------|-------------|
| `event_topic_invalid` | 422 | `EventTopicInvalidError(topic)` | `subscribe` with a topic of neither form |
| `event_topic_refused` | 403 | `EventTopicRefusedError(topic)` | No authorizer admitted the topic |
| `event_subscriptions_exceeded` | 409 | `EventSubscriptionsExceededError(limit)` | The socket already holds `viewsets_event_max_subscriptions` topics |
| `event_cursors_exceeded` | 422 | `EventCursorsExceededError(count, limit)` | A poll carries more cursors than `viewsets_event_max_subscriptions` |

All four are `DfViewSetError`s in `fastapi_viewsets.exceptions`; see [Error Codes](./error-codes).
On `poll`, `event_topic_invalid` and `event_topic_refused` are a topic's `error`, not the status of
the answer.

## Frontend

The frontend event client and `watch()` on a ViewSet are described in [Events (Vue)](./vue-events).
They speak the wire contract on this page: `subscribe`
and `unsubscribe` over muxws, `poll` over either transport, and each push a one-shot stream whose
headers carry `:event`, `:version`, `:topic`, `:rev` and `:prev` and whose payload is the event's
payload. A poll entry carries `rev`, `prev`, `event`, `version` and `payload`.
