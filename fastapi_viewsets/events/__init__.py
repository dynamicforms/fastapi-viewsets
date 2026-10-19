"""
Events: the backend publishes an event to a topic, and every frontend following the topic learns of
it, as a push over its muxws socket or on its next poll.

    from fastapi_viewsets.events import Event, source

    class ImportFinished(Event):
        event_name = "import_finished"

        file: str
        rows: int

    imports = source("imports")

    await imports.publish(ImportFinished(file="members.csv", rows=412), key="7")   # topic "imports:7"

Every event is recorded in the store (`settings.viewsets_event_store`) before it is pushed. The
application calls `start_events()` and `stop_events()` from its lifespan.
"""

from .authorizers import (
    allow_all,
    allow_authenticated,
    authorize_topic,
    DeclaredSources,
    EventAuthorizer,
    ViewsetTopics,
)
from .changes import Change, ChangeEvents, RecordsChanged, routed_base_path, TOPIC_CONTEXT_KEY, write_topic
from .delivery import (
    deliver,
    DeliveryFilter,
    drop_peer,
    drop_subscriptions,
    passes_filters,
    reset_subscriptions,
    subscribe,
    Subscription,
    subscriptions,
    topics_of,
    unsubscribe,
    wait_for_pushes,
)
from .event import Event
from .lifecycle import check_events, start_events, stop_events
from .scopes import (
    event_scope,
    EventScope,
    GATE_RUN_SCOPE_KEY,
    reset_event_scopes,
    resolve_scope,
    resolve_topic,
    ResolvedTopic,
    SCOPE_CONTEXT_KEY,
    scope_of,
    ScopeKey,
    ScopeResolver,
)
from .sources import declared_source, publish, reset_sources, Source, source
from .store import EventStore, get_store, LoggedEvent, MAX_REV, MemoryEventStore
from .topics import (
    is_scoped_topic,
    is_valid_topic,
    is_viewset_topic,
    scoped_topic,
    SOURCE_NAME_PATTERN,
    split_custom_topic,
    split_scoped_topic,
    validate_topic,
)
from .viewset import EventsViewSet, register_events

__all__ = [
    "GATE_RUN_SCOPE_KEY",
    "MAX_REV",
    "SCOPE_CONTEXT_KEY",
    "SOURCE_NAME_PATTERN",
    "TOPIC_CONTEXT_KEY",
    "Change",
    "ChangeEvents",
    "DeclaredSources",
    "DeliveryFilter",
    "Event",
    "EventAuthorizer",
    "EventScope",
    "EventStore",
    "EventsViewSet",
    "LoggedEvent",
    "MemoryEventStore",
    "RecordsChanged",
    "ResolvedTopic",
    "ScopeKey",
    "ScopeResolver",
    "Source",
    "Subscription",
    "ViewsetTopics",
    "allow_all",
    "allow_authenticated",
    "authorize_topic",
    "check_events",
    "declared_source",
    "deliver",
    "drop_peer",
    "drop_subscriptions",
    "event_scope",
    "get_store",
    "is_scoped_topic",
    "is_valid_topic",
    "is_viewset_topic",
    "passes_filters",
    "publish",
    "register_events",
    "reset_event_scopes",
    "reset_sources",
    "reset_subscriptions",
    "resolve_scope",
    "resolve_topic",
    "routed_base_path",
    "scope_of",
    "scoped_topic",
    "source",
    "split_custom_topic",
    "split_scoped_topic",
    "start_events",
    "stop_events",
    "subscribe",
    "subscriptions",
    "topics_of",
    "unsubscribe",
    "validate_topic",
    "wait_for_pushes",
    "write_topic",
]
