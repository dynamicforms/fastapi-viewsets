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
    """Global, mutable settings of fastapi_viewsets, configured once by the application (`settings`)."""

    def __init__(self):
        self.viewsets_context_processors: list[ContextProcessor] = []
        self.viewsets_context_json_encoder: type[json.JSONEncoder] | None = None
        self.viewsets_context_json_decoder: type[json.JSONDecoder] | None = None
        self.viewsets_command_middleware: list[CommandMiddleware] = []
        self.viewsets_idempotency_ttl: float = 86400
        """Seconds an `Idempotency` outcome is kept; a later repeat runs the action again."""
        self.viewsets_idempotency_lock_ttl: float = 600
        """
        Seconds an `Idempotency` running record lives after its last extension (extended every third of
        it); once expired, a repeat runs the action again.
        """
        self.viewsets_auth_processors: list[AuthBackend] = []
        self.default_action_configuration: dict[Any, Any] = {}
        self.default_list_shape: str = "plain"
        """List shape of a viewset that sets none: "plain", "paginated" or "cursor"."""
        self.viewsets_register_muxws: bool = True
        """
        Whether a viewset is published on muxws when neither `route_viewset(register_muxws=)` nor
        `@transports(muxws=)` says. Read at decoration time.
        """
        self.viewsets_security_scheme: SecurityBase | None = None
        """
        Security scheme added to every route for the OpenAPI "Authorize" flow only. Read at decoration
        time, as is `viewsets_command_middleware`.
        """
        self.viewsets_event_store: EventStore | None = None
        """
        The events `EventStore`; None is a per-process `MemoryEventStore`. A deployment of several
        processes (Celery workers included) sets a `RedisEventStore` in every process.
        """
        self.viewsets_event_authorizers: list[EventAuthorizer] = []
        """
        Consulted in order per topic on every `subscribe` and `poll`: the first True admits, the first
        False refuses; no decision (or an empty list) refuses.
        """
        self.viewsets_event_delivery_filters: list[DeliveryFilter] = []
        """
        Decide per event whether a push is sent to a subscription or a poll answers it. False skips;
        a filter that raises counts as True.
        """
        self.viewsets_event_listeners: list[EventListener] = []
        """
        Called with every event in every started process, before pushes, subscribed or not. A raising
        listener is logged and the next one runs.
        """
        self.viewsets_event_log_size: int = 32
        """Most recent events per topic kept for `poll`; with 0 a poll behind the rev answers `events: null`."""
        self.viewsets_event_log_ttl: float = 60.0
        """Seconds a topic's log is kept after its last event; the rev counter never expires."""
        self.viewsets_event_push_timeout: float = 2.0
        """
        Seconds delivery filters plus `peer.notify()` (which only queues) may take for one push to one
        socket; a slower push is cancelled and logged at debug level.
        """
        self.viewsets_event_max_subscriptions: int = 256
        """Topics per socket, and cursors per poll; beyond it `EventSubscriptionsExceededError` (409)."""
        self.viewsets_event_reauthorize_after: float | None = 300.0
        """
        Seconds after which a push to a scoped-topic subscription first reruns the topic's gate. A 4xx
        refusal (not 429) or another store topic drops the subscription; other failures skip only that
        push. None disables it.
        """
        self.viewsets_event_poll_after_ms: int = 0
        """`poll_after_ms` of every poll answer: the shortest client poll interval (ms)."""


settings = Settings()
