"""
The subscribe surface: a viewset with `subscribe`, `unsubscribe` and `poll`, which the application
mounts with `register_events(router, base_path="/events")`. Being a viewset, it passes through the
application's command middleware and context processors and has a `/schema`.
"""

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
from ..exceptions import EventCursorsExceededError, EventTopicInvalidError
from ..mux_ws.transports import transports
from ..routing import PEER_SCOPE_KEY
from . import delivery
from .authorizers import authorize_topic
from .store import get_store
from .topics import is_valid_topic


class TopicRequest(BaseModel):
    """The body of `subscribe` and `unsubscribe`."""

    topic: str


class SubscribeAnswer(BaseModel):
    """The answer of `subscribe`."""

    rev: int
    """The topic's current rev, 0 for a topic without events."""


class PollRequest(BaseModel):
    """The body of `poll`: each topic mapped to the rev the caller holds, or None to start from now."""

    cursors: dict[str, int | None]


class PolledEvent(BaseModel):
    """One event of a poll answer."""

    rev: int
    prev: int
    event: str
    """The event's `event_name`."""
    version: int = Field(ge=1, description="The event's `event_version`: the version of the payload's shape.")
    payload: Any = None


class TopicAnswer(BaseModel):
    """
    The answer of `poll` for one topic. `events` is None when the log cannot answer the cursor; the
    caller then re-reads the state it displays. A topic that was refused, or that has neither form,
    has `rev` and `events` None and `error` set to the code of the refusal. `error` is left out of
    the serialized answer when it is None.
    """

    rev: int | None
    events: list[PolledEvent] | None
    error: str | None = None

    @model_serializer(mode="wrap")
    def _leave_out_no_error(self, handler: SerializerFunctionWrapHandler, _info: SerializationInfo) -> Any:
        data = handler(self)
        if isinstance(data, dict) and data.get("error") is None:
            data.pop("error", None)
        return data

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        # the JSON schema of the fields: the serializer above returns `Any`, which documents nothing
        return handler({key: value for key, value in core_schema.items() if key != "serialization"})


class PollAnswer(BaseModel):
    """The answer of `poll`."""

    topics: dict[str, TopicAnswer]
    """One answer per cursor of the request, in the request's order."""
    poll_after_ms: int
    """`settings.viewsets_event_poll_after_ms`: the shortest interval, in milliseconds, at which the
    caller polls."""


def _peer_of(request: Request, action: str) -> Any:
    peer = request.scope.get(PEER_SCOPE_KEY)
    if peer is None:
        raise RuntimeError(
            f"events: {action} needs the socket the command arrived on; "
            "serve the muxws commands with process_command(..., peer=peer)"
        )
    return peer


def _error_code(error: HTTPException) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code else "event_topic_refused"


class EventsViewSet:
    """
    Subscriptions to event topics over a muxws socket, and polls of event topics over either
    transport. `register_events()` mounts it.
    """

    __router = APIRouter()

    @transports(rest=False, muxws=True)
    @__router.post("/subscribe")
    async def subscribe(self, request: Request, context: Context, body: TopicRequest) -> SubscribeAnswer:
        """
        Subscribes the socket the command arrived on to `topic` and answers the topic's current rev.
        Every event published to the topic after it is pushed to the socket until the socket closes
        or unsubscribes. 422 `event_topic_invalid` for a topic of neither form, 403
        `event_topic_refused`, or the status of the authorizer's `HTTPException`, for a topic the
        authorizers do not admit, 409 `event_subscriptions_exceeded` for a socket that already holds
        `settings.viewsets_event_max_subscriptions` topics. A command served by `process_command`
        without `peer=` raises `RuntimeError`.
        """
        peer = _peer_of(request, "subscribe")
        topic = body.topic
        if not is_valid_topic(topic):
            raise EventTopicInvalidError(topic)
        await authorize_topic(request, context, topic)
        await delivery.subscribe(peer, context, topic)
        revs = await get_store().revs([topic])
        return SubscribeAnswer(rev=revs.get(topic, 0))

    @transports(rest=False, muxws=True)
    @__router.post("/unsubscribe", status_code=204)
    async def unsubscribe(self, request: Request, body: TopicRequest) -> None:
        """
        Removes the socket's subscription to `topic`. Answers 204, also for a topic the socket does
        not hold. A command served by `process_command` without `peer=` raises `RuntimeError`.
        """
        await delivery.unsubscribe(_peer_of(request, "unsubscribe"), body.topic)

    @transports(rest=True, muxws=True)
    @__router.post("/poll")
    async def poll(self, request: Request, context: Context, body: PollRequest) -> PollAnswer:
        """
        Answers every cursor of the request. Per topic the authorizers run; a refused topic answers
        `error` with the refusal's code (`event_topic_refused`, or the `code` of the `HTTPException`
        an authorizer raised, FastAPI's or Starlette's, `event_topic_refused` for one without a
        `code`) and `event_topic_invalid` for a topic of neither form, and the other topics are
        unaffected. A None cursor answers the topic's current rev and no events, a cursor equal
        to it `[]`, and any other cursor the events since it, or `events: null` when the log holds
        no event whose `prev` is the cursor. 422 `event_cursors_exceeded` for more cursors than
        `settings.viewsets_event_max_subscriptions`.
        """
        cursors = body.cursors
        limit = settings.viewsets_event_max_subscriptions
        if len(cursors) > limit:
            raise EventCursorsExceededError(len(cursors), limit)

        answers: dict[str, TopicAnswer] = {}
        admitted: list[str] = []
        for topic in cursors:
            if not is_valid_topic(topic):
                answers[topic] = TopicAnswer(rev=None, events=None, error="event_topic_invalid")
                continue
            try:
                await authorize_topic(request, context, topic)
            except HTTPException as error:
                answers[topic] = TopicAnswer(rev=None, events=None, error=_error_code(error))
                continue
            admitted.append(topic)

        store = get_store()
        revs = await store.revs(admitted) if admitted else {}
        for topic in admitted:
            rev = revs.get(topic, 0)
            cursor = cursors[topic]
            if cursor is None or cursor == rev:
                answers[topic] = TopicAnswer(rev=rev, events=[])
                continue
            logged = await store.since(topic, cursor)
            if logged is None:
                answers[topic] = TopicAnswer(rev=rev, events=None)
                continue
            events = [
                PolledEvent(rev=e.rev, prev=e.prev, event=e.name, version=e.version, payload=e.payload) for e in logged
            ]
            # an event appended between `revs` and `since` is in the chain; the answer's rev covers it
            answers[topic] = TopicAnswer(rev=max([rev, *(e.rev for e in events)]), events=events)

        return PollAnswer(
            topics={topic: answers[topic] for topic in cursors},
            poll_after_ms=settings.viewsets_event_poll_after_ms,
        )


def register_events(router: APIRouter, base_path: str = "/events") -> type[EventsViewSet]:
    """
    Mounts the events viewset on `router` under `base_path`: `POST {base_path}/subscribe` and
    `POST {base_path}/unsubscribe` on muxws only, `POST {base_path}/poll` on REST and muxws. Each
    call decorates a subclass of `EventsViewSet` of its own with `route_viewset(router, base_path)`
    and returns it, so the viewset can be mounted on more than one router and configured with
    `@action_configuration` like any other viewset.
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
