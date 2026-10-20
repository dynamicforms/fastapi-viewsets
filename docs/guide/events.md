# Events

A viewset answers when the frontend asks. `fastapi_viewsets.events` carries the other direction: the
backend publishes an event to a topic, and every frontend following that topic learns of it, as a
push over its [muxws](./muxws) socket or on its next poll.

The package supplies:

- the publish side: `Event` classes, sources of custom topics, `publish()`, and an event store;
- the subscribe side: a viewset with `subscribe`, `unsubscribe` and `poll`, a list of authorizers,
  a list of delivery filters, and the delivery of each event to the subscribed sockets;
- `ChangeEvents`, a [command middleware](./command-middleware) that publishes `RecordsChanged` after
  every create, update and destroy action of a viewset;
- [event scopes](#scoped-topics): the store topic of a viewset's events derived from the request, so
  that one routed path carries the events of each tenant separately.

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

A topic is a string. Matching is exact. A client subscribes to and polls topics of two forms, which
cannot name the same topic:

| Form | Syntax | Declared by | Example |
|------|--------|-------------|---------|
| viewset | `/` followed by the path, the routed base path of a viewset | `route_viewset`, with the `include_router` prefixes | `/api/music` |
| custom | `name` or `name:key`; `name` matches `[a-z0-9_.-]+`, `key` is non-empty | `source(name)` | `imports`, `imports:7` |

Neither form contains whitespace or a control character. A key may contain `:`; `a:b:c` is the
source `a` with the key `b:c`.

A viewset's topic is the path its routes are routed under, `include_router` prefixes included,
which is the `basePath` a frontend ViewSet uses. A path parameter of the prefix or of `base_path`
has its value in the topic: a viewset at `/orgs/{org_id}/tracks` has the topic `/orgs/7/tracks`
for organisation 7, a topic per organisation. A viewset mounted on two routers has two topics,
unless its class carries [`@event_scope`](#scoped-topics), whose routed paths share the store topic
of each scope key. A Starlette `Mount` path in front of the application is not part of the topic.

The store keeps a third form, `@{name}:{key}`, the store topic of a [scoped](#scoped-topics) viewset
topic. The server derives it from the subscribing request; a client that sends it receives
`fastapi-viewsets.event_topic_invalid`.

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

1. raises `ValueError` for a topic that is neither a viewset topic, a custom topic nor a scoped
   topic `@{name}:{key}`, and `TypeError` for an `event` that is not an `Event`; the topic's form is
   all that is checked;
2. `rev, prev = await store.append(topic, event.event_name, payload, event.event_version)`;
3. `await store.publish(topic, rev, prev, event.event_name, payload, event.event_version)`, which
   reaches `deliver()` in every process whose store is started. An exception of this step is logged
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

| | `MemoryEventStore()` | `RedisEventStore.from_url(url)`, `RedisEventStore(redis_client)` |
|-|----------------------|-------------------------------------------------------------------|
| Reach | the process | every process that reaches the Redis |
| `append` | one synchronous step without an `await` | one Lua script |
| `publish` | `deliver()` in the same process | `PUBLISH` on `{prefix}events`; the reader of every started process calls `deliver()` |
| `start` / `stop` | no-op | `SUBSCRIBE` of `{prefix}events` and the reader / closes the reader |

`RedisEventStore.from_url(url, **options)` builds one `redis.asyncio.Redis` client per event loop
(`PerLoopRedis`). A `redis.asyncio` connection belongs to the loop it was opened on, and a process
uses the store on several loops: the server's, the loops of a Celery worker's executions, and the
loop `async_to_sync()` opens for a call without a running loop to hand it to (WSGI, management
commands, plain threads). `options` are `Redis.from_url` options; unless given, a connect may take 2
seconds and a command 5 (`socket_connect_timeout`, `socket_timeout`). `RedisEventStore(redis_client)`
takes one `redis.asyncio.Redis` the application constructs, for a process that uses the store on
one loop only. `RedisEventStore` lives in `fastapi_viewsets.events.redis`, the only module of the
package that uses `redis`. Its keys:

| Key | Content |
|-----|---------|
| `{prefix}rev:{topic}` | The topic's rev, a decimal string without expiry |
| `{prefix}log:{topic}` | The topic's log, a list of JSON entries, newest first, at most `viewsets_event_log_size` long, expiring `viewsets_event_log_ttl` seconds after the topic's last event |
| `{prefix}events` | The pub/sub channel of every event |

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
from fastapi_viewsets.conf import settings
from fastapi_viewsets.events import check_events, start_events, stop_events
from fastapi_viewsets.events.redis import RedisEventStore

settings.viewsets_event_store = RedisEventStore.from_url("redis://localhost")


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

`RedisEventStore.start()` subscribes one pub/sub connection to `{prefix}events` and returns once
Redis has confirmed it, within `subscribe_timeout` seconds (default `5.0`); the reader, one task on
that connection, then calls `deliver()` for every event. `start()` and `stop()` run on the loop the
reader runs on, the server's. `deliver()` pushes an event to the sockets of the process that hold
its topic and leaves out a topic without one, so every process receives every event and no event
reaches a socket that does not hold its topic. A failed read is logged and retried after
`read_timeout` seconds (default `1.0`); a message that cannot be delivered is logged and skipped.
When redis-py reconnects the connection it subscribes the channel again; an event published while
it is down does not reach the process, and the event client finds it by poll, on the topic's next
push or its check poll.

### Processes

An event published in a process that holds no socket, a Celery worker, reaches the sockets through Redis only. With a
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
| `POST {base_path}/subscribe` | muxws | `{"topic": "/api/music"}` | 200 `{"rev": 17}`, the topic's current rev; `{"rev": 17, "scope": "7"}` for a [scoped](#scoped-topics) topic |
| `POST {base_path}/unsubscribe` | muxws | `{"topic": "/api/music"}` | 204, also for a topic the socket does not hold |
| `POST {base_path}/poll` | REST and muxws | `{"cursors": {"/api/music": 17, "imports:7": null}, "scopes": {}}` | 200, [below](#poll) |

The transports are set with `@transports` on each endpoint and do not depend on
`settings.viewsets_register_muxws`.

### `peer=`

A subscription names a socket. `subscribe` and `unsubscribe` read it from
`request.scope["fastapi_viewsets.peer"]`, which `process_command` sets from its `peer` argument:

```python
from fastapi_viewsets.mux_ws import process_command

ALLOWED_ORIGINS = ["https://app.example"]


@app.websocket("/ws")
async def muxws_endpoint(websocket: WebSocket) -> None:
    peer = await accept(websocket, allowed_origins=ALLOWED_ORIGINS)
    peer.on_stream(lambda payload, stream: process_command(payload, stream, connection=websocket, peer=peer))
    await peer.serve()
```

A WebSocket authenticated by a cookie has no CSRF protection; `allowed_origins` makes `accept()`
refuse a foreign `Origin` (see [muxws](./muxws#authentication)).

`subscribe` or `unsubscribe` served without `peer=` raises `RuntimeError` naming
`process_command(..., peer=peer)`; the caller receives a 500, and the log carries the message.

### `subscribe` and `unsubscribe`

`subscribe`:

1. reads the socket; without one, `RuntimeError`;
2. a topic of neither form is 422 `fastapi-viewsets.event_topic_invalid`;
3. runs the [authorizers](#authorization); a refusal is 403 `fastapi-viewsets.event_topic_refused`, or the status of
   the `HTTPException` an authorizer raised;
4. resolves the store topic: the topic itself, or for a [scoped](#scoped-topics) topic
   `@{name}:{key}` from the gate run; a resolver that answers `None` is 403 `fastapi-viewsets.event_scope_missing`;
   a refusal of step 3 or 4 with a status from 400 to 499 other than 429 removes the socket's
   subscription to the topic, if it holds one; a 429 or 5xx leaves it as it is;
5. subscribes the socket to the store topic; a socket that already holds
   `settings.viewsets_event_max_subscriptions` other topics (default `256`) is 409
   `fastapi-viewsets.event_subscriptions_exceeded`;
6. answers the store topic's current rev, read after the subscription is in the table, so every
   event published after the rev the answer carries is pushed to the socket while the store's reader
   receives events, and the scope key for a scoped topic.

A topic the socket already holds is subscribed again with the new call's context and store topic,
and counts once.

Subscriptions are held per process, in a table keyed by socket and topic and delivered by store
topic; the store does not take part in subscribing.

The first subscription of a socket installs one `peer.on_close` handler, which drops every
subscription of the socket. A command runs to completion after its socket is lost; a `subscribe`
that reaches the table after its socket closed (`peer.is_open` false) is not entered in it. A
reconnected peer is a new socket and subscribes again.

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
    "/orgs/7/tags": {"rev": 12, "events": [], "scope": "7"},
    "org:9": {"rev": null, "events": null, "error": "fastapi-viewsets.event_topic_refused", "status": 403}
  },
  "poll_after_ms": 0
}
```

The request carries `cursors`, each topic mapped to the rev the caller holds or `null`, and
`scopes`, the scope key the caller holds for each scoped topic it holds one for (`{}` for none). A
request without `scopes` trusts every cursor.

A poll carrying more than `settings.viewsets_event_max_subscriptions` cursors is 422
`fastapi-viewsets.event_cursors_exceeded`. Otherwise every cursor is answered, in the request's order:

| Topic and cursor | `rev` | `events` | `error`, `status` |
|------------------|-------|----------|-------------------|
| a topic of neither form | `null` | `null` | `fastapi-viewsets.event_topic_invalid`, `422` |
| refused by the authorizers or the gate run | `null` | `null` | `fastapi-viewsets.event_topic_refused`, `fastapi-viewsets.event_scope_missing`, or the `code` of the `DfViewSetError` raised (`fastapi-viewsets.session_expired`, `fastapi-viewsets.not_authorized`, `fastapi-viewsets.rate_limited`, ...); `fastapi-viewsets.event_topic_refused` for an `HTTPException` without a `code`; `status` is the refusal's HTTP status |
| a cursor, `scopes` present, and the topic's entry in `scopes` (absent for none) differs from the scope key the server resolves | the current rev | `null` | absent |
| cursor `null` | the current rev | `[]` | absent |
| cursor equal to the current rev | the current rev | `[]` | absent |
| any other cursor | the current rev | `store.since(store_topic, cursor)`: the events after the cursor that the [delivery filters](#delivery-filters) pass, or `null` when the log cannot answer it | absent |

Every answer of a scoped topic carries `scope`, its scope key; an unscoped topic's answer has none.
`error`, `status` and `scope` are left out of an answer that has none. A refused or invalid topic
does not affect the other topics. The authorizers run for every topic of every poll. A poll over
muxws that refuses a topic with a status from 400 to 499 other than 429 removes the socket's
subscription to it; a 429 or a status of 500 or above is transient and keeps it, as the frontend's
event client counts these statuses. In `events`, each event's `prev`
is the rev of the event before it in the answer, the cursor for the first. `rev` is never lower than
the rev of any event after the cursor, also when an event is appended while the poll is answered,
and covers the events the delivery filters left out.
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
  topic's `error` and its status in the topic's `status` on `poll`. Any other exception is a 500.

An authorizer may also return its decision without an awaitable.

A held subscription of an unscoped topic is not authorized again. An application whose callers lose
access while connected closes their socket (`peer.close()`), or removes the subscriptions with
`drop_subscriptions(where)` in every process, from a [listener](#listeners), and the frontend
subscribes again under its current identity. A subscription of a scoped topic is
[reauthorized](#reauthorization).

### `ViewsetTopics`

`ViewsetTopics()` admits a viewset's topic to a caller the viewset itself admits. For a class with
[`@event_scope`](#scoped-topics) the [gate run](#the-gate-run) decides: a refusal propagates, and a
completed gate run admits the topic. For any other class:

1. A topic that does not start with `/` gets `None`.
2. The viewset is looked up on the routes of `request.app`, `include_router` prefixes included, in
   routing order: the first route whose endpoint carries `__viewset_class__` and, as
   `__viewset_action__`, the class's `list_items`, or `retrieve` for a class without `list_items`,
   and whose path without that action's path in `__viewset_metadata__["paths"]` matches the topic as
   Starlette matches a request path. A path parameter matches one segment of the topic and is
   converted by its convertor: `/orgs/{org_id:int}/tracks` matches `/orgs/7/tracks` with
   `org_id` `7`. No such route gets `None`. A viewset in an application mounted with Starlette's
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

A check in a middleware's `__call__`, in a context processor's answer, or in `perform_*` does not
run for such a class. A class whose read check is there gets it at subscribe time through
`@event_scope`; a class that is not split by tenant uses a constant resolver,
`@event_scope(lambda request, context: "all")`.

An `Authorization` check reads the path parameters from the request it receives:

```python
async def org_member(request: Request, cls: type, context: Context) -> bool:
    return await is_member(await context.user, request.path_params["org_id"])
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

## Scoped topics

A viewset whose rows belong to a tenant (an organisation, a workspace) publishes the changes of each
tenant separately, whichever route the tenant was selected by: a path parameter
(`/orgs/{org_slug}/tags`), a header or the domain (`/api/v1/tags` with `X-Org`), or both at once
while an application moves from one to the other. The class declares the scope of its events:

```python
from fastapi_viewsets.events import event_scope


async def org_scope(request: Request | None, context: Context) -> str | None:
    # SelectedOrg, a middleware of the chain, set org_id only for an organisation the caller belongs to
    return await context.org_id if "org_id" in context else None


@event_scope(org_scope)
@route_viewset(api_router, "/tags", pk_field_name="id")
class TagsViewSet(ViewSetMixin[int, Tag]): ...
```

`event_scope(resolver, *, name=None)` sets `cls.__event_scope__` to `EventScope(name, resolver)`.
`name` defaults to `f"{cls.__module__}.{cls.__qualname__}"` and holds no whitespace, control
character or `:`. Subclasses inherit the scope and its name. The decorator applies before or after
`route_viewset`. A name another class declared raises `ValueError`, unless the class is a subclass
of that class, or has the same module and qualified name without a `<locals>` part (the class of a
reloaded module). Classes a function defines share their module and qualified name
(`make.<locals>.TagsViewSet`), so each one passes a `name` of its own:

```python
def make_viewset(router: APIRouter, model: type[BaseModel], name: str) -> type:
    @event_scope(org_scope, name=f"app.{name}")
    @route_viewset(router, f"/{name}", pk_field_name="id")
    class ModelViewSet(ViewSetMixin[int, model]): ...

    return ModelViewSet
```

### The two names of a topic

| Name | Value | Used by |
|------|-------|---------|
| client topic | the routed base path, `/api/v1/tags` or `/orgs/7/tags` | the client: `subscribe`, `poll`, `:topic` of a push |
| store topic | `@{scope.name}:{key}`, e.g. `@app.tags.TagsViewSet:7` | `publish`, the store, the Redis keys and channel messages |

A client never names a store topic. The server resolves the key:

- on `subscribe` and on every `poll`, in a [gate run](#the-gate-run) of the calling request;
- on a write, in the call that writes, after its whole command chain (see
  [ChangeEvents](#scoped-classes)).

A write through any route of the class or of a subclass, whose resolver answers `7`, publishes to
`@name:7`, and every subscription whose store topic is `@name:7` receives the event, each with its
own client topic in `:topic` and the key in `:scope`. A subscriber of another key does not.

### The resolver

`ScopeResolver` is `resolver(request, context) -> str | int | None`, sync or async. It is the scope's
authorization point: it answers a key only for a scope the caller may read, and raises an
`HTTPException` or answers `None` otherwise. It reads either a value a check of the gate run
established (a middleware's `depends()` or `__call__`, a route dependency, a context processor that
validates it), or the answer of the viewset's own read check called with the context. It reads a
client value such as `request.headers` or `request.path_params` only when a check of the gate run
validated it.

| Answer | Key |
|--------|-----|
| `int` (not `bool`) | `str(key)` |
| `str` without whitespace and control characters | itself |
| `None` | none: subscribe 403 `fastapi-viewsets.event_scope_missing`, poll `error: fastapi-viewsets.event_scope_missing, status: 403`, a write publishes nothing and logs a warning |
| any other value, an empty `str` | `TypeError` / `ValueError` |

An application that normalises the header, the path parameter and the domain into one context value
answers the same key for every route of one tenant.

### The gate run

A gate run is everything a `GET` of the class's gate action (`list_items`, else `retrieve`) by the
caller runs before the action executes, against a request of its own:

1. The request is the calling request with the method `GET`, the topic as its path (under the
   application's `root_path`), the topic's path parameters, no query string, no body, the caller's
   headers without `Idempotency-Key`, `Content-Length` and `Content-Type`, its cookies, client,
   transport, peer and lifespan state, a context of its own, and `GATE_RUN_SCOPE_KEY`
   (`"fastapi_viewsets.events.gate"`) in its scope with the gate action's name. `scope["route"]` is
   the route Starlette sets for a real request to the topic.
2. The route's dependencies run (as for [`ViewsetTopics`](#viewsettopics)); one that does not
   validate refuses the topic with `fastapi-viewsets.event_topic_refused`.
3. The context processors run with the viewset class as `viewset`.
4. `depends()` of every effective middleware runs, then the chain of their `__call__`s with the class
   as `viewset`. In place of the action the scope's resolver runs.
5. A chain that does not reach the resolver, or answers a status of 400 or above, refuses the topic
   with `fastapi-viewsets.event_topic_refused`. A resolver that answers `None` refuses it with `fastapi-viewsets.event_scope_missing`.
   An `HTTPException` propagates with its status; any other exception is a 500 of the subscribe, or
   of the whole poll.

The result's headers, cookies and body are discarded, and the context of the events call is not
changed. A middleware detects a gate run with `request.scope.get(GATE_RUN_SCOPE_KEY)`; `Idempotency`
passes a gate run through, and `RateLimiter` counts it as one call of the gate action. One gate run
answers every topic of one request with the same class, gate action, route and path parameter values.
Its cost is one context build and one run of the middleware chain per such topic, per subscribe and
per poll.

A middleware that selects the tenant runs in the gate run under the configuration of the viewset's
gate action. The events viewset itself may have it switched off, with
`action_configuration({SelectedOrg: False})(events_viewset)`.

### Mixed routes

One class serves the header route and the path route through a subclass:

```python
class OrgTagsViewSet(TagsViewSet):
    pass


route_viewset(APIRouter(prefix="/orgs/{org_slug}"), "/tags", pk_field_name="id")(OrgTagsViewSet)
```

The subclass inherits the scope and its name, so `/api/v1/tags` (with `X-Org: 7`) and
`/orgs/7/tags` share the store topic `@name:7`. Decorating one class with `route_viewset` twice
replaces its `__viewset_metadata__` and its muxws registration; a subclass has its own. A subclass
with a narrower read rule declares a scope of its own, `@event_scope(resolver, name="...")`, or uses a
[delivery filter](#delivery-filters): under the parent's name it receives the parent's pks and ops.

### Reauthorization

A push to a subscription of a scoped topic first runs the topic's gate again with the request the
subscription was made with, once `settings.viewsets_event_reauthorize_after` seconds (default
`300.0`) have passed since the subscribe or the last reauthorization. A gate run that answers the
subscription's store topic sends the push and renews the subscription; a refusal (an `HTTPException`
of a status from 400 to 499 other than 429, `fastapi-viewsets.event_scope_missing` included) or another store topic
removes the subscription and skips the push; any other exception, a 429 or a status of 500 or above
included, skips the push and keeps the subscription for the next push to try again. Concurrent pushes share one
gate run, and the push timeout bounds the wait for it. `None` turns it off. An application that
revokes a membership removes the subscriptions at once, in every process, by publishing an event
whose [listener](#listeners) calls:

```python
from fastapi_viewsets.events import drop_subscriptions

drop_subscriptions(lambda s: s.scope == str(org_id) and s.context.raw().get("user_id") == user_id)
```

### Edge cases

| Case | Behaviour |
|------|-----------|
| Resolver answers `None` | subscribe 403 `fastapi-viewsets.event_scope_missing`; poll `error: fastapi-viewsets.event_scope_missing`, `status: 403`; a write runs and publishes nothing, with a warning |
| Resolver raises on a write | the write is refused before the action; the resolver runs after the whole command chain. A `celery_viewset` call of any action is refused the same way, before the dispatch |
| Membership revoked | the next subscribe or poll refuses the topic and removes the server subscription; pushes are gated again after `viewsets_event_reauthorize_after`; `drop_subscriptions(where)` removes them at once |
| Scope change of a client (header, domain) | the client calls `refresh()`; it refreshes a topic itself on a push after its `headers()` changed; a poll's `scopes` that differ resync the topic and subscribe it again |
| Client sends `@...` | 422, or poll `fastapi-viewsets.event_topic_invalid` |
| Frontend before 0.11.2 against a scoped topic | subscribe works and `:scope` is ignored; a poll without `scopes` trusts the cursor. Scoped topics need the frontend of 0.11.2 or later |
| Action configuration differs between the gate action and a write | the keys can differ, and the event goes to a store topic no subscriber holds; the resolver's inputs are set for both the gate action and the write actions |
| Endpoint that writes outside the eight actions | publishes to `await write_topic(self, request, context)` (see [Custom endpoints](#custom-endpoints)) |
| Write outside a request | `await publish(scope_of(TagsViewSet).topic(key), RecordsChanged(...))` |
| Unscoped viewsets, custom sources | the client topic is its own store topic; no gate run |

`resolve_topic(request, topic)` answers `ResolvedTopic(topic, store_topic, scope)` for any client
topic and is what `subscribe` and `poll` use; `scope_of(cls)` answers a class's `EventScope`;
`resolve_scope(scope, request, context)` calls a resolver and normalises its answer.

---

## Delivery

`deliver(topic, rev, prev, name, payload, version)` is the entry point into a process's
subscriptions; both stores call it. `topic` is a store topic. For each subscription delivered under
it in the process it starts one task and returns without waiting for them, so a slow delivery filter
for one socket does not delay the others. A task:

1. runs the subscription's [reauthorization](#reauthorization) when it is due; a refusal skips the
   socket;
2. runs the [delivery filters](#delivery-filters) with the subscription's client topic; a `False`
   skips the socket;
3. sends one push, a one-shot stream (one `open` frame with `end` set), which is not answered:

   ```python
   await peer.notify(
       payload,
       headers={":event": name, ":version": version, ":topic": subscription.topic, ":rev": rev, ":prev": prev},
   )
   ```

   with `":scope": subscription.scope` added for a scoped topic.

The steps together are bounded by `settings.viewsets_event_push_timeout` seconds (default `2.0`).
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

`topic` is the client topic. `subscription` carries `peer`, `context`, `topic` (the client topic),
`store_topic` and `scope` (the scope key of a scoped topic, else `None`):

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

### Listeners

```python
from typing import Any

from fastapi_viewsets.events import drop_subscriptions


def revoke_membership(topic: str, rev: int, prev: int, name: str, payload: Any, version: int) -> None:
    if name == "membership_removed":
        drop_subscriptions(
            lambda s: s.scope == payload["club"] and s.context.raw().get("user_id") == payload["user_id"]
        )


settings.viewsets_event_listeners = [revoke_membership]
```

`EventListener` is `listener(topic, rev, prev, event_name, payload, version) -> None`, sync or
async; `topic` is the store topic. `deliver()` calls the listeners in list order with every event,
awaiting each, in every process whose store is started, whether or not the process holds a
subscription of the topic, and pushes the event after the last one. A listener that raises is logged
and the next one runs. A listener runs on the reader's path of every event, so it returns quickly.

`drop_subscriptions` removes the subscriptions of its own process. A listener on an event the
application publishes is how every process removes them: with a `RedisEventStore` the event reaches
the reader of every started process, and each process's listener removes its own subscriptions,
before the event's own pushes. The list is empty by default.

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
`"fastapi_viewsets.events.topic"` (`TOPIC_CONTEXT_KEY`); for a [scoped class](#scoped-classes) it
also stores the deferred resolution of the scope key of the call. `routed_base_path(request)` computes
the routed base path:
the request's path, relative to the application's `root_path`, with as many trailing segments
removed as the action's path in `__viewset_metadata__["paths"]` has. Each path parameter of that
path matches one segment; a `{name:path}` parameter is not supported. Path parameters of the prefix
and of `base_path` keep their values: `/orgs/7/tracks` for a request to `/orgs/7/tracks/1`.

For a class without `@event_scope`, `execute()` publishes to, in order:

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

### Scoped classes

For a class with [`@event_scope`](#scoped-topics):

- `__call__` stores, for every action of the class, custom endpoints included, the deferred
  resolution of the key with the call's request and context under `"fastapi_viewsets.events.scope"`
  (`SCOPE_CONTEXT_KEY`). It does not for a write action (the eight of the table) configured `False`
  or with a `topic`.
- The resolver runs once, after the whole command chain, wherever `ChangeEvents` sits in it: for an
  action that executes in the web process, on the first read of the entry (`execute()` of a write
  action, `write_topic()`), and not at all for an action that reads neither; in the web process
  before the dispatch for every `celery_viewset` action. The dispatched context then carries
  `{"name": scope.name, "key": key}`, or `None` for a key of `None`, and the worker does not call the
  resolver. An exception of the resolver refuses the call before the action; a `celery_viewset`
  task is not sent.
- `execute()` publishes to `topic_for(execution)` of a subclass that overrides `topic_for`; else, for
  an execution a request called, `@{name}:{key}` of the key under `SCOPE_CONTEXT_KEY`; else
  `@{name}:{key}` of the key the resolver answers in `execute()` with the execution's request and
  context (an execution dispatched outside any call or in the execution of another action). A key of
  `None` publishes nothing and logs a warning naming the class, the action and the scope.
- A `topic` option raises `TypeError`: the scope and the option exclude each other.

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

`topic_for` returns the topic per the order above for a class without `@event_scope`; an override in
a subclass is used for every class, scoped ones included, and may return a scoped topic.
`changes_for` returns the changes of one execution per the table above.

### Where it runs

| Part | In-process action | `celery_viewset` action |
|------|-------------------|-------------------------|
| `__call__`, which stores the topic | FastAPI process | FastAPI process |
| the resolver of a scoped class | FastAPI process, in `execute()` or `write_topic()` | FastAPI process, before the dispatch |
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

An endpoint that writes outside the eight actions publishes itself, to the topic `write_topic`
answers:

```python
from fastapi_viewsets.events import Change, RecordsChanged, publish, write_topic


@__router.post("/{pk}/archive")
async def archive(self, request: Request, context: Context, pk: int) -> None:
    await self.perform_archive(context, pk)
    topic = await write_topic(self, request, context)
    if topic is not None:
        await publish(topic, RecordsChanged(op="deleted", changes=[Change(pk=pk)]))
```

`await write_topic(viewset, request, context)`, `viewset` a class or an instance:

| Class | Topic |
|-------|-------|
| with `@event_scope` | `@{name}:{key}` of the key under `SCOPE_CONTEXT_KEY`, which `ChangeEvents.__call__` stores for every action of the class, else of the key the resolver answers with `request` and `context`; `None` for a key of `None`; an exception of the resolver propagates |
| without | the routed base path under `TOPIC_CONTEXT_KEY`, else `__viewset_metadata__["base_path"]`, else `None` |

In a `celery_viewset` worker `write_topic` does not call the resolver: the key is the one the web
process resolved before the dispatch, carried in the dispatched context. A context without an entry
of the class's scope (an action dispatched outside any call, or in the execution of an action of
another scoped class) answers `None`.

---

## Settings

| Setting | Default | Meaning |
|---------|---------|---------|
| `viewsets_event_store` | `None` | The `EventStore`; `None` is one `MemoryEventStore` per process |
| `viewsets_event_authorizers` | `[]` | The authorizers, in order; an empty list refuses every topic |
| `viewsets_event_delivery_filters` | `[]` | The delivery filters, in order |
| `viewsets_event_listeners` | `[]` | The listeners, in order; called with every event in every started process, before its pushes |
| `viewsets_event_log_size` | `32` | Events kept per topic for `poll` |
| `viewsets_event_log_ttl` | `60.0` | Seconds a topic's log is kept after its last event; the rev is kept without expiry |
| `viewsets_event_push_timeout` | `2.0` | Seconds the delivery filters and `peer.notify()` of one push to one socket may take together |
| `viewsets_event_max_subscriptions` | `256` | Topics one socket may hold, and cursors one poll may carry |
| `viewsets_event_poll_after_ms` | `0` | `poll_after_ms` of every poll answer, in milliseconds |
| `viewsets_event_reauthorize_after` | `300.0` | Seconds after which a push to a subscription of a scoped topic runs its gate again; `None` never |

Every one is read on use. See [settings](../api/settings).

## Errors

| Code | Status | Class | Raised when |
|------|--------|-------|-------------|
| `fastapi-viewsets.event_topic_invalid` | 422 | `EventTopicInvalidError(topic)` | `subscribe` with a topic of neither form |
| `fastapi-viewsets.event_topic_refused` | 403 | `EventTopicRefusedError(topic)` | No authorizer admitted the topic; or in a gate run a route dependency did not validate, the command chain did not reach the resolver, or it answered a status of 400 or above |
| `fastapi-viewsets.event_scope_missing` | 403 | `EventScopeMissingError(topic)` | The scope resolver of a scoped topic answered `None` for the caller |
| `fastapi-viewsets.event_subscriptions_exceeded` | 409 | `EventSubscriptionsExceededError(limit)` | The socket already holds `viewsets_event_max_subscriptions` topics |
| `fastapi-viewsets.event_cursors_exceeded` | 422 | `EventCursorsExceededError(count, limit)` | A poll carries more cursors than `viewsets_event_max_subscriptions` |

All five are `DfViewSetError`s in `fastapi_viewsets.exceptions`; see [Error Codes](./error-codes). On `poll`,
`fastapi-viewsets.event_topic_invalid`, `fastapi-viewsets.event_topic_refused` and
`fastapi-viewsets.event_scope_missing` are a topic's `error`, with the status in its `status`, not the status of the
answer.

## Frontend

The frontend event client and `watch()` on a ViewSet are described in [Events (Vue)](./vue-events).
They speak the wire contract on this page: `subscribe`
and `unsubscribe` over muxws, `poll` over either transport, and each push a one-shot stream whose
headers carry `:event`, `:version`, `:topic`, `:rev`, `:prev` and, for a scoped topic, `:scope`, and
whose payload is the event's payload. A poll entry carries `rev`, `prev`, `event`, `version` and
`payload`.
