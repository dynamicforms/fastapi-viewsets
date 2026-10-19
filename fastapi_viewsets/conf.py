import json

from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.security.base import SecurityBase

    from .context.auth import AuthBackend
    from .events.authorizers import EventAuthorizer
    from .events.delivery import DeliveryFilter, EventListener
    from .events.store import EventStore
    from .middleware import CommandMiddleware

ContextProcessor = Callable[["Request", Any], Awaitable[dict[str, Any]]]


class Settings:
    """
    Global, mutable settings for fastapi_viewsets - configured once by the application, e.g.:

        from fastapi_viewsets.conf import settings

        async def auth_processor(request, viewset) -> dict:
            return {"user": await authenticate(request)}

        settings.viewsets_context_processors = [auth_processor]
    """

    def __init__(self):
        self.viewsets_context_processors: list[ContextProcessor] = []
        self.viewsets_context_json_encoder: type[json.JSONEncoder] | None = None
        self.viewsets_context_json_decoder: type[json.JSONDecoder] | None = None
        self.viewsets_command_middleware: list[CommandMiddleware] = []
        self.viewsets_idempotency_ttl: float = 86400
        """
        Seconds an outcome recorded by `fastapi_viewsets.middleware.idempotency.Idempotency` is kept
        after it was stored. A repeat after that runs the action again.
        """
        self.viewsets_idempotency_lock_ttl: float = 600
        """
        Seconds a running record of `Idempotency` is kept after it was last extended; the process
        running the call extends it every third of this time. Once it expires, a repeat runs the
        action again.
        """
        self.viewsets_auth_processors: list[AuthBackend] = []
        self.default_action_configuration: dict[Any, Any] = {}
        self.default_list_shape: str = "plain"
        """
        The shape a list endpoint answers in when the viewset does not say - "plain", "paginated"
        or "cursor". An application that pages everything sets it once here instead of on every
        viewset.
        """
        self.viewsets_register_muxws: bool = True
        """
        Whether a viewset is reachable over the muxws transport (see fastapi_viewsets/mux_ws/) when
        it does not say either way itself. `route_viewset(register_muxws=...)` overrides this per
        viewset, and `@transports(muxws=...)` overrides it again per endpoint - each
        level only decides for the levels below it that left the question open (None). Read at
        decoration time, so it must be set before any viewset class is decorated.
        """
        self.viewsets_security_scheme: SecurityBase | None = None
        """
        Purely for OpenAPI/Swagger discoverability (a lock icon + testable Authorize flow) - attached
        as an extra sub-dependency alongside the per-route Middleware.depends() bridge (see
        route_viewset.py), not otherwise consumed by this library. Must be set before any viewset
        class is decorated with route_viewset, as `viewsets_command_middleware` must, which decides
        `disable_response_model` when the routes are built.
        """
        self.viewsets_event_store: EventStore | None = None
        """
        The `EventStore` of `fastapi_viewsets.events`: it assigns each event its rev, keeps the
        topic's log for `poll` and carries the event to `deliver()` in every process whose store
        is started. None is one `MemoryEventStore` for this process. A deployment of more than one
        process, a Celery worker included, sets a `RedisEventStore` in every process.
        """
        self.viewsets_event_authorizers: list[EventAuthorizer] = []
        """
        The functions that decide whether a caller may follow a topic, consulted in list order on
        every `subscribe` and on every `poll`, per topic. The first `True` admits the topic, the first
        `False` refuses it, and no decision refuses it: an empty list refuses every topic.
        """
        self.viewsets_event_delivery_filters: list[DeliveryFilter] = []
        """
        The functions that decide, per subscription and per event, whether a push is sent, and per
        poll and per event whether the poll answers the event. A filter that answers False skips that
        socket or leaves the event out of the poll answer; a filter that raises counts as True.
        """
        self.viewsets_event_listeners: list[EventListener] = []
        """
        The functions called with every event in every process whose store is started, before its
        pushes, whether or not the process holds a subscription of its topic. A listener that raises is
        logged and the next one runs.
        """
        self.viewsets_event_log_size: int = 32
        """
        The number of a topic's most recent events the store keeps for `poll`. 0 keeps none, and a
        poll from any cursor behind the topic's rev answers `events: null`.
        """
        self.viewsets_event_log_ttl: float = 60.0
        """
        Seconds a topic's log is kept after the topic's last event. The topic's rev counter is kept
        without expiry. `RedisEventStore` sets it as the log key's expiry; `MemoryEventStore` empties
        an expired log when it next reads or appends to it.
        """
        self.viewsets_event_push_timeout: float = 2.0
        """
        Seconds the delivery filters and `peer.notify()` of one push of one event to one socket may
        take together. `notify()` queues the push on the socket and returns without waiting for the
        socket to send it. A push that takes longer is cancelled and logged at debug level.
        """
        self.viewsets_event_max_subscriptions: int = 256
        """
        The number of topics one socket may hold; a subscription beyond it raises
        `EventSubscriptionsExceededError` (409). Also the number of cursors one poll may carry.
        """
        self.viewsets_event_reauthorize_after: float | None = 300.0
        """
        Seconds after which a push to a subscription of a scoped viewset topic first runs the
        topic's gate again with the request the subscription was made with. The push is sent when the
        gate answers the subscription's store topic; a refusal (an `HTTPException` of a status from
        400 to 499 other than 429) or another store topic removes the subscription and skips the
        push; any other failure, a 429 or 5xx included, skips the push and keeps the subscription for
        the next push to try again. None never runs it. Subscriptions of unscoped topics are not
        affected.
        """
        self.viewsets_event_poll_after_ms: int = 0
        """
        `poll_after_ms` of every poll answer: the shortest interval, in milliseconds, at which a
        client polls.
        """


settings = Settings()
