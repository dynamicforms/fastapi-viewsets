# settings — API Reference

```python
from fastapi_viewsets.conf import settings
```

`settings` is the one instance of `fastapi_viewsets.conf.Settings`. The application assigns its
attributes once, before any viewset class is decorated with `route_viewset`.

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `viewsets_context_processors` | `list[ContextProcessor]` | `[]` | Build each request's context; see [Context Processors](../guide/context-processors) |
| `viewsets_context_json_encoder` | `type[json.JSONEncoder] \| None` | `None` | Encodes context values that cross the Celery boundary |
| `viewsets_context_json_decoder` | `type[json.JSONDecoder] \| None` | `None` | Decodes them on the worker |
| `viewsets_command_middleware` | `list[CommandMiddleware]` | `[]` | Wraps every request-facing execution; see [Command Middleware](../guide/command-middleware) |
| `viewsets_idempotency_ttl` | `float` | `86400` | Seconds an outcome recorded by `Idempotency` is kept; see [Idempotent Calls](../guide/idempotency) |
| `viewsets_idempotency_lock_ttl` | `float` | `600` | Seconds a running record of `Idempotency` is kept after it was last extended; the process running the call extends it every third of this time |
| `viewsets_auth_processors` | `list[AuthBackend]` | `[]` | Tried in order to resolve `context.user`; see [Authentication](../guide/authentication) |
| `default_action_configuration` | `dict[Any, Any]` | `{}` | The configuration every action starts from; see [Action Configuration](../guide/action-configuration) |
| `default_list_shape` | `str` | `"plain"` | The list shape of a viewset that states none: `"plain"`, `"paginated"` or `"cursor"` |
| `viewsets_register_muxws` | `bool` | `True` | Whether a viewset is published on muxws when neither it nor its endpoint says; see [muxws](../guide/muxws#choosing-which-viewsets-are-published) |
| `viewsets_security_scheme` | `SecurityBase \| None` | `None` | A FastAPI security scheme added to every route, for the OpenAPI "Authorize" flow only |
| `viewsets_event_store` | `EventStore \| None` | `None` | The event store; `None` is one `MemoryEventStore` per process. A deployment of more than one process sets a `RedisEventStore` in every process; see [Events](../guide/events#the-store) |
| `viewsets_event_authorizers` | `list[EventAuthorizer]` | `[]` | Decide whether a caller may follow a topic, in list order; the first `True` admits, the first `False` refuses, no decision refuses |
| `viewsets_event_delivery_filters` | `list[DeliveryFilter]` | `[]` | Decide per subscription and per event whether a push is sent, and per poll and per event whether the poll answers the event; `False` skips the socket or leaves the event out of the poll answer, an exception counts as `True` |
| `viewsets_event_log_size` | `int` | `32` | Events kept per topic for `poll` |
| `viewsets_event_log_ttl` | `float` | `60.0` | Seconds a topic's log is kept after its last event; the topic's rev is kept without expiry |
| `viewsets_event_push_timeout` | `float` | `2.0` | Seconds the delivery filters and `peer.notify()` of one push to one socket may take together; `notify()` queues the push and does not wait for the socket |
| `viewsets_event_max_subscriptions` | `int` | `256` | Topics one socket may hold, and cursors one poll may carry |
| `viewsets_event_poll_after_ms` | `int` | `0` | `poll_after_ms` of every poll answer, the shortest interval a client polls at |

`ContextProcessor` is `Callable[[Request, Any], Awaitable[dict[str, Any]]]`; a processor may also
take a `config` keyword, see [Action Configuration](../guide/action-configuration).

`viewsets_register_muxws` and `viewsets_security_scheme` are read when a viewset is decorated, and
so is whether an entry of `viewsets_command_middleware` declares `modifies_response_shape`: changed
later, they do not reach viewsets already decorated. The middleware list itself, the processors,
the auth backends, the idempotency TTLs and the `viewsets_event_*` settings are read on every use.

## Other public classes

| Name | Module | Description |
|------|--------|-------------|
| `SaveState` | `fastapi_viewsets.save_state.save_state` | Base of a viewset that loads and saves its state around each call: `load_state()`, `save_state()`; see [ViewSet Lifecycle](../guide/lifecycle) |
| `SaveStateRedis` | `fastapi_viewsets.save_state.save_state` | `SaveState` backed by one Redis key, `save_state_redis_key` |
| `SerializeState`, `SerializeStateSlots` | `fastapi_viewsets.save_state.serialize_state` | `serialize_state()` / `deserialize_state(state)`; the second serialises the slots `__slots__` names |
| `NotFoundResponse` | `fastapi_viewsets.response_classes` | The documented model of a 404 body, `{"detail": "Item with pk {pk} not found"}`; `NOT_FOUND_RESPONSE` is the `responses` entry the mixins attach |
