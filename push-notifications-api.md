# Push notifications API

Status: concept. Nothing described here is implemented yet.

## Purpose

A viewset serves data when the frontend asks for it (pull). This API is the opposite direction: the
backend announces that something happened, and every frontend subscribed to it learns of it. Both
transports fastapi-viewsets already has carry it — over REST the frontend polls, over muxws the
backend pushes — behind one API on each side.

The library supplies base classes and primitives only, for the backend and the frontend. Which
events exist, what they carry, which tags address them and what a frontend does on arrival are the
application's. A recipe for the most common family, "data behind a viewset changed", lives in the
cookbook section below, not in the library.

## Vocabulary

| Term | Meaning |
|---|---|
| **event class** | A declared message type, parametrized by its payload model. The same name and payload on both sides: on the backend the class pushes and is never instantiated, on the frontend an instance is one subscription. |
| **tag** | A string `kind:value` (`club:bled-ski-club`, `activity:7`) or a bare `kind` (`system`). |
| **tag set** | The non-empty set of tags an event is pushed with, or a subscription is made with. |
| **queue** | One event class together with one tag set. The unit of storage, subscription and cursor. |
| **sequence id** | The id of one event within its queue, assigned by Redis (`<ms since epoch>-<seq>`). Strictly increasing within a queue. |
| **tag authorizer** | An application function that decides whether the current caller may receive events addressed with a given tag. |

## Rules

1. **An event is pushed with at least one tag.** `push()` with an empty tag set raises.
2. **Every tag kind has a registered tag authorizer.** `push()` with an unregistered kind raises, and
   a subscription containing one is refused. A tag nobody may be refused (`system`) is registered
   with an explicit allow-all authorizer, so "public" is always a declaration and never a default.
3. **Matching is exact.** A subscription receives an event when the event class is the same and the
   tag sets are equal.
4. **Authorization runs on every delivery.** Each tag of the subscription goes through its authorizer
   for every event pushed and every poll answered. A caller who loses access to a club stops
   receiving that club's events on the next delivery, with no revocation step anywhere.
5. **An event with no current subscribers reaches nobody over push**, and stays readable over REST
   for the queue's retention time.
6. **A subscription starts from now.** Subscribing returns the queue's latest sequence id and no
   history.
7. **A gap is reported by the backend.** When events newer than the caller's cursor have already been
   trimmed away, the answer says so for that queue. The frontend cannot detect this itself: sequence
   ids are not contiguous, and an event a subscriber was not authorized for is indistinguishable
   from a trimmed one.
8. **Both transports carry the same sequence ids.** A frontend can move between push and poll at any
   time without losing or double-applying an event.

## Backend

### Declaring and pushing an event

```python
from pydantic import BaseModel

from fastapi_viewsets.events import Event


class RecordDeletedPayload(BaseModel):
    pk: int


class RecordDeleted(Event[RecordDeletedPayload]):
    pass


await RecordDeleted.push({"club:bled-ski-club", "activity"}, {"pk": 7})
```

An event class is never instantiated. `push(tags, content)` is a classmethod: it validates `content`
(a payload model instance or a dict) against the payload model and publishes it to the queue named
by the class and `tags`.

`push()` is transaction-agnostic: it publishes immediately. An event that describes a database
change is pushed after the transaction commits, otherwise a frontend reacting to it can read the state
from before the change (see the cookbook).

### Tag authorizers

```python
from fastapi_viewsets.context import Context
from fastapi_viewsets.events import allow_authenticated, tag_authorizer


@tag_authorizer("club")
async def club_member(context: Context, value: str) -> bool:
    return await is_member(context["user"], club_slug=value)


tag_authorizer("system")(allow_authenticated)
```

An authorizer receives the same `Context` a viewset action receives, populated by
`settings.viewsets_context_processors`, on both transports: over muxws the context is built from the
WebSocket handshake headers, exactly as for any other command.

### Endpoints

The API is published as a viewset, so it goes through the application's command middleware and
appears in the OpenAPI schema like any other.

| Call | REST | muxws | Request | Answer |
|---|---|---|---|---|
| subscribe | `POST {prefix}/subscribe` | stream, stays open | list of `{event, tags}` | per queue: `{event, tags, last_id}`, or a refusal (unknown tag kind, authorizer said no) |
| poll | `POST {prefix}/poll` | — | list of `{event, tags, after}` | `{events: [...], gaps: [{event, tags}]}` |
| unsubscribe | — (REST keeps no subscription state) | cancel the stream | | |

One poll answers every queue the frontend follows in one response, events ordered by sequence id.
The order is exact within a queue; across queues it follows the millisecond part of the id.

An event on the wire, on both transports:

```json
{"event": "RecordDeleted", "tags": ["activity", "club:bled-ski-club"], "id": "1727500000417-0", "payload": {"pk": 7}}
```

### Storage

- One Redis stream per queue. The key is derived from the event name and the sorted tag set.
- `XADD` assigns the sequence id. Entries older than the retention time are trimmed (`MINID`), and the
  key expires one retention period after its last `push()`, so queues for one-off tags such as
  `activity:7` do not accumulate.
- The latest sequence id for `subscribe` is the stream's `last-generated-id`; a queue with no stream
  yet answers `0-0`.
- Gap detection is one comparison: the caller's cursor is older than the stream's
  `max-deleted-entry-id` (Redis 7+).
- Retention is set by the application, globally and per event class. It must be several poll
  intervals long; browsers throttle timers in background tabs, so a tab returning to the foreground
  after a long absence receives a gap as a normal case.

### Push fan-out

A muxws subscription is held by the web process that holds the socket. Each web process reads the
streams of the queues its own sockets are subscribed to and pushes new entries to those subscribers,
after running rule 4 for each. A process reads only the queues it currently has subscribers for.

## Frontend

```ts
import { Event, configureEvents } from '@dynamicforms/fastapi-viewsets/events';

configureEvents({ axiosInstance, peer, pollInterval: 5000 });

interface RecordDeletedPayload {
  pk: number;
}

abstract class RecordDeleted extends Event<RecordDeletedPayload> {
  static readonly eventName = 'RecordDeleted';
}

class ActivityDeleted extends RecordDeleted {
  constructor(club: string, private readonly rows: Ref<Activity[]>) {
    super([`club:${club}`, 'activity']);
  }

  onEvent(payload: RecordDeletedPayload) {
    this.rows.value = this.rows.value.filter((row) => row.id !== payload.pk);
  }

  onGap() {
    reloadActivities();
  }
}

// in a component
const subscription = new ActivityDeleted('bled-ski-club', rows);
onMounted(() => subscription.subscribe());
onUnmounted(() => subscription.unsubscribe());
```

- The event class carries the wire name and the payload type, the same as on the backend. An
  instance is one subscription: one tag set, given to the constructor, and the reaction to events
  arriving in it.
- The payload arrives as the argument of `onEvent`, never as fields of the instance: one instance
  receives many events.
- `onEvent` and `onGap` are abstract. A subscription class states what it does with an event and
  what it does when the backend reports a gap, even where the answer is an empty body. What "start
  over" means depends on the screen, and a gap handled by nothing is a screen that silently stops
  matching the server.
- Where the reaction gets its state from (constructor arguments, a class declared inside the
  component) is the application's.
- `configureEvents()` is called once. All subscriptions of the application share one client: one
  poll answers every queue, and one muxws stream carries every pushed event.
- With a healthy `peer`, events arrive over muxws and polling is suspended. Without one, or while it
  is disconnected, the client polls every `pollInterval` milliseconds. On every switch the client
  polls once from the last sequence id it holds for each subscription, and discards any event whose
  id is not newer than that.
- `peer` is optional and accepts the same values as `muxwsViewSet`'s. A deployment with no muxws is
  a complete deployment, with poll latency.

## Cookbook: "data behind a viewset changed"

The library ships no such event; this is the recipe for one. One generic event covers every table;
the table is a tag, so a subscription follows exactly one table.

```python
class RecordChangedPayload(BaseModel):
    op: Literal["created", "updated", "deleted"]
    pk: int
    record: dict | None      # whatever the application wants the frontend to have


class RecordChanged(Event[RecordChangedPayload]):
    pass


tag_authorizer("member")(allow_authenticated)   # the club tag decides who receives it


class MemberViewSet(DjangoORMViewSet[int, MemberSchema]):
    async def perform_create(self, context: Context, data: MemberSchema) -> MemberSchema:
        member = await create_member(club_id, data)
        record = MemberSchema.model_validate(member)
        push_on_commit(
            RecordChanged,
            {f"club:{club_slug}", "member"},
            {"op": "created", "pk": member.pk, "record": record.model_dump()},
        )
        return record
```

`push_on_commit` is the application's: it defers `push()` until the surrounding transaction
commits.

```ts
abstract class RecordChanged<T> extends Event<RecordChangedPayload<T>> {
  static readonly eventName = 'RecordChanged';
}

class MemberChanged extends RecordChanged<Member> {
  constructor(club: string, private readonly rows: Ref<Member[]>) {
    super([`club:${club}`, 'member']);
  }

  onEvent({ op, pk, record }: RecordChangedPayload<Member>) {
    const others = this.rows.value.filter((row) => row.id !== pk);
    this.rows.value = op === 'deleted' ? others : [...others, record!];
  }

  onGap() {
    reloadMembers();
  }
}
```

`rows` is the array the grid receives as its data prop. Where the grid holds the whole data set and
sorts and filters locally, placing an inserted or updated record is a grid operation that needs
nothing from the server.

A payload carries the same data to every subscriber of its queue. An event that includes the record
therefore suits data that every authorized subscriber may see in full; an event carrying only `op`
and `pk`, followed by a `retrieve` on the frontend, serializes per caller.

## Prerequisites in fastapi-viewsets

- **Long-lived muxws actions.** Over muxws a viewset action today answers once and ends. A
  subscription is a stream that stays open and carries server-sent frames until either side cancels
  it.
- **Redis as a dependency of the events module.** The rest of the library does not need Redis; this
  module does, both for storage and for fan-out.

## Open questions

- Whether authorizer results may be cached for a short time on the push path, where rule 4 costs one
  authorizer call per subscriber per event.
- How an event class names itself on the wire: an explicit `eventName` on both sides, or a name
  derived from the class.
- The default retention time.
- Whether a process reads its queues with one blocking `XREAD` over all of them, re-issued whenever
  the set changes, or with a separate pub/sub notification per queue.
