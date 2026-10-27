"""
The events viewset (`subscribe`, `unsubscribe`, `poll`), mounted with `register_events()`. It passes
through the application's command middleware and context processors like any viewset.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from pydantic import (
    BaseModel,
    Field,
    GetJsonSchemaHandler,
    model_serializer,
    SerializationInfo,
    SerializerFunctionWrapHandler,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema
from starlette.exceptions import HTTPException

from ..conf import settings
from ..context import Context
from ..decorators.route_viewset import route_viewset
from ..error_responses import ApiErrorBody, error_body
from ..exceptions import EventCursorsExceededError, EventTopicInvalidError
from ..mux_ws.transports import transports
from ..routing import PEER_SCOPE_KEY
from . import delivery
from .authorizers import authorize_topic
from .scopes import _empty_body, _GATE_CACHE_SCOPE_KEY, resolve_topic, ResolvedTopic
from .store import get_store
from .topics import is_valid_topic


def _leave_out_none(data: Any, names: tuple[str, ...]) -> Any:
    if isinstance(data, dict):
        for name in names:
            if data.get(name) is None:
                data.pop(name, None)
    return data


class TopicRequest(BaseModel):
    """The body of `subscribe` and `unsubscribe`."""

    topic: str


class SubscribeAnswer(BaseModel):
    """The answer of `subscribe`. `scope` is left out of the serialized answer when it is None."""

    rev: int
    """The topic's current rev, 0 for a topic without events."""
    scope: str | None = None
    """The scope key of a scoped viewset topic; None for an unscoped topic."""

    @model_serializer(mode="wrap")
    def _leave_out_no_scope(self, handler: SerializerFunctionWrapHandler, _info: SerializationInfo) -> Any:
        return _leave_out_none(handler(self), ("scope",))

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        # the JSON schema of the fields: the serializer above returns `Any`, which documents nothing
        return handler({key: value for key, value in core_schema.items() if key != "serialization"})


class PollRequest(BaseModel):
    """The body of `poll`: each topic mapped to the rev the caller holds, or None to start from now."""

    cursors: dict[str, int | None]
    scopes: dict[str, str] | None = None
    """The scope key the caller holds per topic; a cursor whose scope key differs from the server's answers
    `events: null`. None trusts every cursor."""


class PolledEvent(BaseModel):
    """One event of a poll answer."""

    rev: int
    prev: int
    """The rev of the event before it in the answer; for the first event, the cursor."""
    event: str
    """The event's `event_name`."""
    version: int = Field(ge=1, description="The event's `event_version`: the version of the payload's shape.")
    payload: Any = None


class TopicAnswer(BaseModel):
    """
    The answer of `poll` for one topic. `events` is None when the log cannot answer the cursor; the
    caller then re-reads its state. A refused or invalid topic has `rev` and `events` None and `error`
    and `status` set. `error`, `status` and `scope` are left out when None.
    """

    rev: int | None
    events: list[PolledEvent] | None
    error: ApiErrorBody | None = None
    status: int | None = None
    """The HTTP status of the refusal; set on every answer with `error`."""
    scope: str | None = None
    """The scope key of a scoped viewset topic; None for an unscoped topic."""

    @model_serializer(mode="wrap")
    def _leave_out_absent(self, handler: SerializerFunctionWrapHandler, _info: SerializationInfo) -> Any:
        return _leave_out_none(handler(self), ("error", "status", "scope"))

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        # the JSON schema of the fields: the serializer above returns `Any`, which documents nothing
        return handler({key: value for key, value in core_schema.items() if key != "serialization"})


class PollAnswer(BaseModel):
    """The answer of `poll`."""

    topics: dict[str, TopicAnswer]
    """One answer per cursor of the request, in the request's order."""
    poll_after_ms: int
    """The shortest interval, in milliseconds, at which the caller polls."""


def _peer_of(request: Request, action: str) -> Any:
    peer = request.scope.get(PEER_SCOPE_KEY)
    if peer is None:
        raise RuntimeError(
            f"events: {action} needs the socket the command arrived on; "
            "serve the muxws commands with process_command(..., peer=peer)"
        )
    return peer


def _reauthorizer(request: Request, topic: str) -> Callable[[], Awaitable[str]]:
    """Re-runs the gate on a copy of the subscribing request (fresh gate cache) and answers the store topic."""
    snapshot = {name: value for name, value in request.scope.items() if name != _GATE_CACHE_SCOPE_KEY}
    snapshot_state = {name: value for name, value in (request.scope.get("state") or {}).items() if name != "_context"}

    async def reauthorize() -> str:
        copy = Request(dict(snapshot, state=dict(snapshot_state)), _empty_body)
        return (await resolve_topic(copy, topic)).store_topic

    return reauthorize


class EventsViewSet:
    """Subscriptions to event topics over a muxws socket, and polls over either transport."""

    __router = APIRouter()

    @transports(rest=False, muxws=True)
    @__router.post("/subscribe")
    async def subscribe(self, request: Request, context: Context, body: TopicRequest) -> SubscribeAnswer:
        """
        Subscribes the socket to `topic` until it closes or unsubscribes, and answers the topic's current
        rev (and scope key for a scoped topic). Later events are pushed to the socket.

        Errors: 422 `fastapi-viewsets.event_topic_invalid` (malformed topic); 403
        `fastapi-viewsets.event_topic_refused` or the authorizer's `HTTPException`; 403
        `fastapi-viewsets.event_scope_missing` (scope resolver answers None); 409
        `fastapi-viewsets.event_subscriptions_exceeded` (`settings.viewsets_event_max_subscriptions`
        reached). A 4xx refusal other than 429 also drops an existing subscription to the topic.
        """
        peer = _peer_of(request, "subscribe")
        topic = body.topic
        if not is_valid_topic(topic):
            raise EventTopicInvalidError(topic)
        try:
            await authorize_topic(request, context, topic)
            resolved = await resolve_topic(request, topic)
        except Exception as error:
            if delivery._is_refusal(error):
                await delivery.unsubscribe(peer, topic)
            raise
        reauthorize = _reauthorizer(request, topic) if resolved.scope is not None else None
        await delivery.subscribe(
            peer, context, topic, store_topic=resolved.store_topic, scope=resolved.scope, reauthorize=reauthorize
        )
        revs = await get_store().revs([resolved.store_topic])
        return SubscribeAnswer(rev=revs.get(resolved.store_topic, 0), scope=resolved.scope)

    @transports(rest=False, muxws=True)
    @__router.post("/unsubscribe", status_code=204)
    async def unsubscribe(self, request: Request, body: TopicRequest) -> None:
        """Removes the socket's subscription to `topic`. Answers 204, also for a topic the socket does not hold."""
        await delivery.unsubscribe(_peer_of(request, "unsubscribe"), body.topic)

    @transports(rest=True, muxws=True)
    @__router.post("/poll")
    async def poll(self, request: Request, context: Context, body: PollRequest) -> PollAnswer:
        """
        Answers every cursor: a None cursor gets the current rev and no events, any other the events
        since it, or `events: null` when the log cannot answer it (or its `scopes` entry differs from
        the server's scope key). Events refused by a delivery filter are left out; `prev` chains the
        events returned and `rev` covers all of them.

        A refused topic answers `error` (an `ApiErrorBody`) and `status`: 403
        `fastapi-viewsets.event_topic_refused` or `fastapi-viewsets.event_scope_missing`, 422
        `fastapi-viewsets.event_topic_invalid`, or the authorizer's `HTTPException`; other topics are
        unaffected. Over muxws a 4xx refusal other than 429 drops the socket's
        subscription. 422 `fastapi-viewsets.event_cursors_exceeded` for more cursors than
        `settings.viewsets_event_max_subscriptions`.
        """
        cursors = body.cursors
        limit = settings.viewsets_event_max_subscriptions
        if len(cursors) > limit:
            raise EventCursorsExceededError(len(cursors), limit)

        peer = request.scope.get(PEER_SCOPE_KEY)
        answers: dict[str, TopicAnswer] = {}
        admitted: list[ResolvedTopic] = []
        for topic in cursors:
            if not is_valid_topic(topic):
                invalid = EventTopicInvalidError(topic)
                answers[topic] = TopicAnswer(
                    rev=None, events=None, error=error_body(invalid), status=invalid.status_code
                )
                continue
            try:
                await authorize_topic(request, context, topic)
                resolved = await resolve_topic(request, topic)
            except HTTPException as error:
                answers[topic] = TopicAnswer(rev=None, events=None, error=error_body(error), status=error.status_code)
                if peer is not None and delivery._is_refusal(error):
                    await delivery.unsubscribe(peer, topic)
                continue
            admitted.append(resolved)

        store = get_store()
        revs = await store.revs(list({resolved.store_topic: None for resolved in admitted})) if admitted else {}
        for resolved in admitted:
            topic, store_topic, key = resolved.topic, resolved.store_topic, resolved.scope
            rev = revs.get(store_topic, 0)
            cursor = cursors[topic]
            if cursor is not None and body.scopes is not None and body.scopes.get(topic) != key:
                answers[topic] = TopicAnswer(rev=rev, events=None, scope=key)
                continue
            if cursor is None or cursor == rev:
                answers[topic] = TopicAnswer(rev=rev, events=[], scope=key)
                continue
            logged = await store.since(store_topic, cursor)
            if logged is None:
                answers[topic] = TopicAnswer(rev=rev, events=None, scope=key)
                continue
            subscription = delivery.Subscription(peer, context, topic, store_topic, key)
            events: list[PolledEvent] = []
            prev = cursor
            for e in logged:
                if await delivery.passes_filters(subscription, topic, e.rev, e.name, e.payload, e.version):
                    events.append(PolledEvent(rev=e.rev, prev=prev, event=e.name, version=e.version, payload=e.payload))
                    prev = e.rev
            # an event appended between `revs` and `since` is in the chain; the answer's rev covers it
            answers[topic] = TopicAnswer(rev=max([rev, *(e.rev for e in logged)]), events=events, scope=key)

        return PollAnswer(
            topics={topic: answers[topic] for topic in cursors},
            poll_after_ms=settings.viewsets_event_poll_after_ms,
        )


def register_events(router: APIRouter, base_path: str = "/events") -> type[EventsViewSet]:
    """
    Mounts the events viewset on `router`: `subscribe` and `unsubscribe` on muxws only, `poll` on REST
    and muxws. Returns a fresh `EventsViewSet` subclass per call, so it can be mounted more than once.
    """
    viewset = type(
        EventsViewSet.__name__,
        (EventsViewSet,),
        {
            "__module__": EventsViewSet.__module__,
            "__qualname__": EventsViewSet.__qualname__,
            "__doc__": EventsViewSet.__doc__,
        },
    )
    return route_viewset(router, base_path)(viewset)
