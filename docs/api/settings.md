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
| `viewsets_auth_processors` | `list[AuthBackend]` | `[]` | Tried in order to resolve `context.user`; see [Authentication](../guide/authentication) |
| `default_action_configuration` | `dict[Any, Any]` | `{}` | The configuration every action starts from; see [Action Configuration](../guide/action-configuration) |
| `default_list_shape` | `str` | `"plain"` | The list shape of a viewset that states none: `"plain"`, `"paginated"` or `"cursor"` |
| `viewsets_register_muxws` | `bool` | `True` | Whether a viewset is published on muxws when neither it nor its endpoint says; see [muxws](../guide/muxws#choosing-which-viewsets-are-published) |
| `viewsets_security_scheme` | `SecurityBase \| None` | `None` | A FastAPI security scheme added to every route, for the OpenAPI "Authorize" flow only |

`ContextProcessor` is `Callable[[Request, Any], Awaitable[dict[str, Any]]]`; a processor may also
take a `config` keyword, see [Action Configuration](../guide/action-configuration).

`viewsets_register_muxws` and `viewsets_security_scheme` are read when a viewset is decorated, and
so is whether an entry of `viewsets_command_middleware` declares `modifies_response_shape`: changed
later, they do not reach viewsets already decorated. The middleware list itself, the processors and
the auth backends are read on every request.

## Other public classes

| Name | Module | Description |
|------|--------|-------------|
| `SaveState` | `fastapi_viewsets.save_state.save_state` | Base of a viewset that loads and saves its state around each call: `load_state()`, `save_state()`; see [ViewSet Lifecycle](../guide/lifecycle) |
| `SaveStateRedis` | `fastapi_viewsets.save_state.save_state` | `SaveState` backed by one Redis key, `save_state_redis_key` |
| `SerializeState`, `SerializeStateSlots` | `fastapi_viewsets.save_state.serialize_state` | `serialize_state()` / `deserialize_state(state)`; the second serialises the slots `__slots__` names |
| `NotFoundResponse` | `fastapi_viewsets.response_classes` | The documented model of a 404 body, `{"detail": "Item with pk {pk} not found"}`; `NOT_FOUND_RESPONSE` is the `responses` entry the mixins attach |
