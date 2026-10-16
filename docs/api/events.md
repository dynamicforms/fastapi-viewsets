# events — API Reference

```python
from fastapi_viewsets.events import (
    Change,
    ChangeEvents,
    DeclaredSources,
    Event,
    EventsViewSet,
    MemoryEventStore,
    RecordsChanged,
    Source,
    Subscription,
    ViewsetTopics,
    allow_all,
    allow_authenticated,
    check_events,
    publish,
    register_events,
    source,
    start_events,
    stop_events,
)
from fastapi_viewsets.events.redis import RedisEventStore
```

The behaviour is described in [Events](../guide/events).

## Events and publishing

### Event

```python
class Event(BaseModel):
    event_name: ClassVar[str]
    event_version: ClassVar[int] = 1
```

The base of every event. A subclass sets `event_name` and declares the payload as pydantic fields.
`event_version` is the version of the payload's shape: `1` unless the subclass sets a larger `int`,
inherited by a subclass. It travels with every push and every poll entry, so a consumer tells the
shapes of one event's payload apart and handles several versions side by side. A subclass without a
non-empty string `event_name`, or with an `event_version` that is not an `int` of at least `1` (a
`bool` is not), raises `TypeError` when the class is created. Both are class attributes, set by plain
assignment or as a `ClassVar`; a subclass that annotates either one as a field
(`event_version: int = 2`) raises `TypeError` when the class is created. The payload is
`model_dump(mode="json")`.

### Functions

| Name | Description |
|------|-------------|
| `async publish(topic: str, event: Event) -> tuple[int, int]` | Validates `topic`, records the event's `event_name`, payload and `event_version` with `store.append`, carries it to the watching processes with `store.publish`, and returns `(rev, prev)`. Raises `ValueError` for a topic of neither form and `TypeError` for an `event` that is not an `Event`; an exception of `store.publish` is logged at warning level and not raised. `OverflowError` of `store.append` propagates |
| `source(name: str, authorizer: EventAuthorizer \| None = None) -> Source` | `Source(name, authorizer)` |
| `declared_source(name: str) -> Source \| None` | The source declared as `name` in this process |
| `reset_sources() -> None` | Forgets every declared source; for tests |

### Source

```python
class Source:
    def __init__(self, name: str, authorizer: EventAuthorizer | None = None): ...
```

The declared owner of the topics `name` and `name:key`. Creating one registers `name` in the
process; a name that does not match `[a-z0-9_.-]+`, or one already declared, raises `ValueError`.

| Member | Description |
|--------|-------------|
| `name` | The source's name |
| `authorizer` | The `EventAuthorizer` `DeclaredSources()` consults for the source's topics, or `None` |
| `topic(key: str \| int \| None = None) -> str` | `name:key`, or `name` without a key. Raises `ValueError` when the result is not a topic |
| `async publish(event: Event, key: str \| int \| None = None) -> tuple[int, int]` | `publish(self.topic(key), event)` |
| `async drop(key: str \| int) -> None` | Removes the rev and the log of `name:key` from the store; its next event, if any, carries `prev` `0` |

## Topics

| Name | Description |
|------|-------------|
| `SOURCE_NAME_PATTERN` | `re.compile(r"[a-z0-9_.-]+")`, the form of a source name, matched in full |
| `is_viewset_topic(topic: Any) -> bool` | Whether `topic` is a string of the viewset form: `/` followed by characters other than whitespace and control characters |
| `is_valid_topic(topic: Any) -> bool` | Whether `topic` has the viewset form or the custom form, `name` or `name:key` with a non-empty `key` without whitespace or control characters |
| `validate_topic(topic: Any) -> str` | `topic`; raises `ValueError` when it has neither form |
| `split_custom_topic(topic: str) -> tuple[str, str \| None] \| None` | `(name, key)` of a custom topic, `key` `None` without one; `None` for a topic of any other form |

## The store

### EventStore

```python
@runtime_checkable
class EventStore(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def append(self, topic: str, name: str, payload: Any, version: int) -> tuple[int, int]: ...
    async def revs(self, topics: list[str]) -> dict[str, int]: ...
    async def since(self, topic: str, after: int) -> list[LoggedEvent] | None: ...
    async def publish(self, topic: str, rev: int, prev: int, name: str, payload: Any, version: int) -> None: ...
    async def drop(self, topic: str) -> None: ...
    async def watch(self, topic: str) -> None: ...
    async def unwatch(self, topic: str) -> None: ...
```

| Method | Contract |
|--------|----------|
| `start()` | Starts what the store needs to carry events to this process |
| `stop()` | Stops what `start()` started |
| `append(topic, name, payload, version)` | Sets the topic's rev to `max(rev + 1, now in milliseconds)`, keeps the event's name, payload and version in the topic's log and returns `(rev, prev)`. Atomic; raises `OverflowError` when the rev would reach `MAX_REV` |
| `revs(topics)` | The current rev of each topic, `0` for a topic without one |
| `since(topic, after)` | `[]` when the topic's rev is `after`; otherwise the kept event whose `prev` is `after` and every newer one, oldest first; `None` when no kept event has that `prev` |
| `publish(topic, rev, prev, name, payload, version)` | Carries the event to `deliver(topic, rev, prev, name, payload, version)` in every process that watches the topic |
| `drop(topic)` | Removes the topic's rev and log |
| `watch(topic)` / `unwatch(topic)` | Starts / stops carrying the topic's events to this process |

### LoggedEvent

```python
class LoggedEvent(NamedTuple):
    rev: int
    prev: int
    name: str
    payload: Any
    version: int
```

One event of a topic's log. `prev` is the topic's rev before the event, `0` for a topic without one;
`version` is the event's `event_version`.

### MemoryEventStore

```python
class MemoryEventStore:
    def __init__(self) -> None: ...
```

The event store of one process. `publish` calls `deliver()` in this process only. A topic's log holds
its last `settings.viewsets_event_log_size` events and is emptied when it is read or appended to more
than `settings.viewsets_event_log_ttl` seconds after the topic's last event. A topic's rev is kept
until `drop`. `append` reads and writes the rev without an `await` in between. `start`, `stop`,
`watch` and `unwatch` do nothing.

### RedisEventStore

```python
from fastapi_viewsets.events.redis import RedisEventStore

class RedisEventStore:
    def __init__(self, redis_client: redis.asyncio.Redis, key_prefix: str = "events:"): ...
```

An event store shared by every process that reaches one Redis. `fastapi_viewsets.events.redis` is
the only module of the package that uses `redis`; `fastapi_viewsets.events` does not import it.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `redis_client` | `redis.asyncio.Redis` | — | The client the application constructs |
| `key_prefix` | `str` | `"events:"` | The prefix of the keys and channels |

| Member | Description |
|--------|-------------|
| `rev_key(topic) -> str` | `{prefix}rev:{topic}`: the topic's rev, a decimal string without expiry |
| `log_key(topic) -> str` | `{prefix}log:{topic}`: the topic's log, a list of JSON entries `{"rev", "prev", "event": {"name", "payload", "version"}}`, newest first, at most `viewsets_event_log_size` long, expiring `viewsets_event_log_ttl` seconds after the topic's last event (`PEXPIRE`, at least 1 ms). A log size of `0` deletes the log |
| `channel(topic) -> str` | `{prefix}ch:{topic}`: the channel `publish` publishes the JSON object `{"topic", "rev", "prev", "name", "payload", "version"}` on |
| `reader_running: bool` | Whether the reader task runs |
| `async start()` | Opens the reader: one task on one pub/sub connection, subscribed to the channel of every watched topic, which calls `deliver()` for each message. A store whose reader runs returns at once |
| `async stop()` | Cancels the reader and closes its connection. The watched topics are kept, and `start()` subscribes them again |
| `async append(topic, name, payload, version)` | One Lua script: the rev with `GET` and `SET`, the log with `LPUSH`, `LTRIM` and `PEXPIRE`. Raises `OverflowError` at `MAX_REV`, changing nothing |
| `async revs(topics)` | `MGET`; an absent key is `0` |
| `async since(topic, after)` | `GET` of the rev, then `LRANGE` of the log |
| `async publish(topic, rev, prev, name, payload, version)` | `PUBLISH` on `channel(topic)` |
| `async drop(topic)` | `DEL` of the rev and the log |
| `async watch(topic)` | Records the topic for `start()`. While the reader runs, `SUBSCRIBE` on the reader's connection, unless the channel is subscribed, and returns once the reader has read Redis's confirmation of that `SUBSCRIBE`, or of the one still unconfirmed. Raises `asyncio.TimeoutError` when no confirmation arrives within 5 seconds, and `ConnectionError` when the reader stops first |
| `async unwatch(topic)` | `UNSUBSCRIBE` on the reader's connection while the reader runs; forgets the topic for `start()` |

The reader logs a failed read and retries after one second, and logs and skips a message that
cannot be delivered.

### Functions and constants

| Name | Description |
|------|-------------|
| `get_store() -> EventStore` | `settings.viewsets_event_store`, or this process's one `MemoryEventStore`, created on first use, while that is `None` |
| `MAX_REV` | `2**53`, the bound every rev stays below |
| `next_rev(prev: int, now_ms: int) -> int` | `max(prev + 1, now_ms)`; raises `OverflowError` when that reaches `MAX_REV`. In `fastapi_viewsets.events.store` |

## Lifespan

| Name | Description |
|------|-------------|
| `async start_events() -> None` | `await get_store().start()` |
| `async stop_events() -> None` | `await get_store().stop()` |
| `check_events() -> bool` | Logs a warning naming `await start_events()` and returns `False` when the store's `reader_running` is `False` (a `RedisEventStore` before `start_events()`); returns `True` otherwise |

## Subscriptions and delivery

### Subscription

```python
@dataclass(frozen=True, eq=False)
class Subscription:
    peer: Any
    context: Context
    topic: str
```

One socket following one topic. `peer` is the muxws peer of the socket, `context` the context of the
`subscribe` call that made the subscription.

### DeliveryFilter

```python
DeliveryFilter = Callable[[Subscription, str, int, str, Any, int], bool | Awaitable[bool]]
```

`filter(subscription, topic, rev, event_name, payload, version)`, sync or async; `version` is the
event's `event_version`. A result that `is False`
skips the push to the subscription's socket; any other result passes; an exception counts as `True`
and is logged at debug level.

### Functions

| Name | Description |
|------|-------------|
| `async deliver(topic: str, rev: int, prev: int, name: str, payload: Any, version: int) -> None` | Starts one task per subscription of `topic` in this process and returns without waiting. A task runs the delivery filters, then `peer.notify(payload, headers={":event": name, ":version": version, ":topic": topic, ":rev": rev, ":prev": prev})`, together bounded by `settings.viewsets_event_push_timeout` seconds; muxws's `notify()` queues the push and does not wait for the socket. A `ConnectionLost` (matched by class name) drops every subscription of its socket; any other failure is logged at debug level |
| `async subscribe(peer: Any, context: Context, topic: str) -> Subscription` | Subscribes the socket to `topic` and returns once the store watches the topic; a topic the socket holds is subscribed again with `context` and counts once. Raises `EventSubscriptionsExceededError` when the socket already holds `settings.viewsets_event_max_subscriptions` other topics. The first subscription of a topic calls `store.watch(topic)`; when that raises, the subscription is removed and the exception propagates. A call made while the topic's `watch` or `unwatch` runs waits for it to end and then calls `store.watch(topic)` itself when the topic is not watched. The first subscription of a socket installs one `peer.on_close` handler, which calls `drop_peer(peer)`. For a socket whose `is_open` is false it returns the subscription without entering it in the table or calling the store |
| `async unsubscribe(peer: Any, topic: str) -> None` | Removes the socket's subscription to `topic`, if it holds one. The last subscription of a topic calls `store.unwatch(topic)`; an exception it raises is logged |
| `drop_peer(peer: Any) -> None` | Removes every subscription of the socket; the topics left without subscriptions are unwatched in a background task |
| `subscriptions(topic: str) -> list[Subscription]` | The subscriptions of `topic` in this process |
| `topics_of(peer: Any) -> list[str]` | The topics the socket holds |
| `async wait_for_pushes() -> None` | Waits until the background tasks of the delivery module (pushes, unwatches) running now and started meanwhile are done |
| `reset_subscriptions() -> None` | Forgets every subscription and watch of this process without calling the store; for tests |

## Authorization

### EventAuthorizer

```python
EventAuthorizer = Callable[[Request, Context, str], Awaitable[bool | None]]
```

`await authorizer(request, context, topic)`: `True` admits the topic, `False` refuses it, `None`
leaves the decision to the next authorizer in `settings.viewsets_event_authorizers`. An
`HTTPException` it raises, FastAPI's or Starlette's, refuses the topic with that exception.

### Authorizers and functions

| Name | Description |
|------|-------------|
| `async authorize_topic(request: Request, context: Context, topic: str) -> None` | Runs `settings.viewsets_event_authorizers` in order and returns at the first `True`. The first `False`, or no decision, raises `EventTopicRefusedError`. An exception an authorizer raises propagates. An authorizer may return its decision without an awaitable |
| `async allow_all(request, context, topic) -> bool` | `True` |
| `async allow_authenticated(request, context, topic) -> bool \| None` | `True` when `await context.user` is truthy; `None` otherwise and for a context without `user` |
| `DeclaredSources()` | `async __call__(request, context, topic) -> bool \| None`: the answer of the `authorizer` of the source that declared the topic's name; `None` for a viewset topic, an undeclared name, and a source without an authorizer |
| `ViewsetTopics()` | `async __call__(request, context, topic) -> bool \| None`: `None` for a topic that does not start with `/` and for one no viewset on `request.app` has (`match_viewset`). Otherwise the checks run against `request` carrying the topic's path parameters as `path_params`: the route's `dependencies` other than `route_viewset`'s own (those of the application, of `include_router`, of the `APIRouter` and of the route), resolved as FastAPI resolves them, `False` when a parameter of one does not validate; then the found action's `@action_configuration` is resolved, the context rebound as `Context(context.raw(), action_configuration=..., action_name=...)`, and `depends(request, cls, context)` of every `Middleware` in `settings.viewsets_command_middleware` and of the instances the configuration injects runs. Returns `True` when none raises. An `HTTPException` propagates |
| `match_viewset(app, topic: str) -> ViewsetMatch \| None` | In `fastapi_viewsets.events.authorizers`. The first route on `app` in routing order, `include_router` prefixes included, whose endpoint carries `__viewset_class__` and, as `__viewset_action__`, the class's `list_items`, or `retrieve` for a class without `list_items`, and whose path without that action's `__viewset_metadata__["paths"]` entry matches `topic` as Starlette matches a request path, path parameters included. Routes of an application under a Starlette `Mount` are not searched |
| `ViewsetMatch` | In `fastapi_viewsets.events.authorizers`. A frozen dataclass: `viewset_class: type`, `action: str`, `route` (the application's route of `action`) and `path_params: dict[str, Any]`, the values of the routed base path's path parameters read from the topic and converted by their convertors |
| `find_viewset(app, topic: str) -> tuple[type, str] \| None` | In `fastapi_viewsets.events.authorizers`. `(viewset_class, action)` of `match_viewset(app, topic)`, or `None` |

## The events viewset

| Name | Description |
|------|-------------|
| `register_events(router: APIRouter, base_path: str = "/events") -> type[EventsViewSet]` | Decorates a new subclass of `EventsViewSet` with `route_viewset(router, base_path)` and returns it |
| `EventsViewSet` | The viewset `register_events` mounts. Not decorated itself |

| Endpoint | Transports | Body | Answer | Errors |
|----------|------------|------|--------|--------|
| `subscribe` — `POST {base_path}/subscribe` | muxws | `TopicRequest` | 200 `SubscribeAnswer` | 422 `event_topic_invalid`, 403 `event_topic_refused` or the authorizer's `HTTPException`, 409 `event_subscriptions_exceeded`, `RuntimeError` (500) without `peer=` |
| `unsubscribe` — `POST {base_path}/unsubscribe` | muxws | `TopicRequest` | 204 | `RuntimeError` (500) without `peer=` |
| `poll` — `POST {base_path}/poll` | REST, muxws | `PollRequest` | 200 `PollAnswer` | 422 `event_cursors_exceeded`; a topic's refusal is in its `TopicAnswer.error` |

`subscribe` and `unsubscribe` read the socket from `request.scope["fastapi_viewsets.peer"]`
(`fastapi_viewsets.routing.PEER_SCOPE_KEY`), set by `process_command(..., peer=peer)`.

The models, in `fastapi_viewsets.events.viewset`:

| Model | Fields |
|-------|--------|
| `TopicRequest` | `topic: str` |
| `SubscribeAnswer` | `rev: int`, the topic's current rev, `0` for a topic without events |
| `PollRequest` | `cursors: dict[str, int \| None]`, each topic mapped to the rev the caller holds, `None` to start from the current rev |
| `PolledEvent` | `rev: int`, `prev: int`, `event: str` (the `event_name`), `version: int` (the `event_version`, at least `1`), `payload: Any = None` |
| `TopicAnswer` | `rev: int \| None`, `events: list[PolledEvent] \| None`, `error: str \| None = None`; `error` is left out of the serialized answer when it is `None` |
| `PollAnswer` | `topics: dict[str, TopicAnswer]`, one per cursor in the request's order; `poll_after_ms: int`, `settings.viewsets_event_poll_after_ms` |

## ChangeEvents

```python
class ChangeEvents(Middleware): ...
```

A [`Middleware`](../guide/command-middleware#class-based-middleware-middleware). `modifies_response_shape`
is `False`.

| Method | Description |
|--------|-------------|
| `async __call__(request, viewset, context, call_next) -> ViewSetResult` | Stores `routed_base_path(request)` in the context under `TOPIC_CONTEXT_KEY` when it is not `None`, then returns `await call_next()` |
| `async execute(execution, call_next) -> Any` | For the eight write actions, unless disabled or `replaced_for(execution)`: resolves and validates the topic and, for `created` and `updated`, the pk field, then runs `call_next()` and publishes one `RecordsChanged`. Returns the action's result. An exception of the publish, `store.append`'s included, propagates after the action has run. Any other action, a disabled one and a replaced one pass through |
| `replaced_for(execution) -> bool` | Whether the execution's action configuration has `ChangeEvents` instances as keys and this instance is not one of them |
| `config_from(context) -> Any` | The value configured under this instance as the key, else under its class |
| `options_for(context) -> dict[str, Any] \| None` | `{}` for `None` and `True`, `None` for `False`, the dict for a dict of `record` and `topic`. Raises `TypeError` for any other value or key |
| `topic_for(execution) -> str` | The configured `topic` (a callable is called with `execution`); else, when `execution.parent` is `None`, `context["fastapi_viewsets.events.topic"]`; else `__viewset_metadata__["base_path"]`. Raises `TypeError` with none of the three |
| `pk_field_for(execution) -> str` | `__viewset_metadata__["pk_field_name"]`, else the viewset's `pk_field_name`, else its `pk_field`. Raises `TypeError` naming the viewset with none of the three |
| `changes_for(execution, op: str, result: Any) -> list[Change]` | For `deleted`, one `Change` per pk of the `pk` argument (keyword or positional); otherwise one per returned record, its pk read from the `pk_field_for` field, with `record` when the options set `record`. A `ViewSetResult` contributes its `body`. A record without the pk field raises `TypeError` |

| `execution.action` | `op` |
|--------------------|------|
| `create`, `bulk_create` | `created` |
| `update`, `partial_update`, `bulk_update`, `bulk_partial_update` | `updated` |
| `destroy`, `bulk_destroy` | `deleted` |

Configuration, read with `config_from`:

| Value | Effect |
|-------|--------|
| `None`, `True` | Defaults |
| `False` | No event |
| `{"record": bool}` | `created` and `updated` changes carry the record as `model_dump(mode="json")` (a record that is not a model: `pydantic_core.to_jsonable_python`); default `False` |
| `{"topic": str \| Callable[[ActionExecution], str]}` | The topic to publish to |

A `ChangeEvents` instance as a key injects that instance for the viewset or action; it reads the
value under its own key, else under its class, and the `ChangeEvents` of
`settings.viewsets_command_middleware` passes the action through (`replaced_for`).

### Models and helpers

| Name | Description |
|------|-------------|
| `Change` | `BaseModel`: `pk: Any`, `record: dict[str, Any] \| None = None` |
| `RecordsChanged` | `Event` with `event_name = "records_changed"` and `event_version = 1`: `op: Literal["created", "updated", "deleted"]`, `changes: list[Change]` |
| `TOPIC_CONTEXT_KEY` | `"fastapi_viewsets.events.topic"` |
| `routed_base_path(request: Request) -> str \| None` | The `include_router` prefixes followed by `base_path` of the viewset whose endpoint the request was routed to, with the values of the request's path parameters: the request's path relative to `root_path`, with as many trailing segments removed as `__viewset_metadata__["paths"][action]` has. `None` when the matched endpoint is not one `route_viewset` registered |

## Settings

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `settings.viewsets_event_store` | `EventStore \| None` | `None` | The store; `None` is one `MemoryEventStore` per process |
| `settings.viewsets_event_authorizers` | `list[EventAuthorizer]` | `[]` | The authorizers, in order |
| `settings.viewsets_event_delivery_filters` | `list[DeliveryFilter]` | `[]` | The delivery filters, in order |
| `settings.viewsets_event_log_size` | `int` | `32` | Events kept per topic for `poll` |
| `settings.viewsets_event_log_ttl` | `float` | `60.0` | Seconds a topic's log is kept after its last event |
| `settings.viewsets_event_push_timeout` | `float` | `2.0` | Seconds the delivery filters and `peer.notify()` of one push to one socket may take together |
| `settings.viewsets_event_max_subscriptions` | `int` | `256` | Topics one socket may hold, and cursors one poll may carry |
| `settings.viewsets_event_poll_after_ms` | `int` | `0` | `poll_after_ms` of every poll answer |

## Errors

| Class | Status | `code` | `params` |
|-------|--------|--------|----------|
| `EventTopicInvalidError(topic)` | 422 | `event_topic_invalid` | `{topic}` |
| `EventTopicRefusedError(topic)` | 403 | `event_topic_refused` | `{topic}` |
| `EventSubscriptionsExceededError(limit)` | 409 | `event_subscriptions_exceeded` | `{limit}` |
| `EventCursorsExceededError(count, limit)` | 422 | `event_cursors_exceeded` | `{count, limit}` |

All four are `DfViewSetError`s in `fastapi_viewsets.exceptions`; see [Error Codes](../guide/error-codes).
