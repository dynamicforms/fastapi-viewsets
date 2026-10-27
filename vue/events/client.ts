/**
 * The event client: follows topics the BE publishes events to, with pushes over a muxws peer and
 * polls over the peer or over axios. Per topic it holds the rev of the last applied event and applies
 * an event only when its `prev` is that rev; a gap is answered by one poll, a poll the BE's log cannot
 * answer is a resync. Without an open peer every followed topic is polled on the ladder (poll.ts).
 *
 * The client's calls carry no `Idempotency-Key`: a `subscribe` answered from a stored outcome would
 * not subscribe the socket it travelled on.
 */

import { isApiErrorBody, ViewSetRequestError } from '../errors';
import type { MuxwsPeerLike } from '../muxws-proxy';
import { readHeaders, readStatus } from '../muxws-reply';

import { DEFAULT_POLL_SETTINGS, type Draw, jitterMultiplier, PollLadder, type PollSettings } from './poll';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** Carries the payload type of an `EventDefinition`; never present at run time. */
declare const PAYLOAD: unique symbol;

/**
 * An event name and, as a type only, the shape of its payload. `P` covers the payloads of every
 * version of the event that a subscription to it handles.
 */
export interface EventDefinition<P> {
  /** The BE event's `event_name`. */
  readonly name: string;
  readonly [PAYLOAD]?: P;
}

/**
 * The event named `name`, whose payload has the type `P`. A subscription receives every version of
 * the event, so `P` describes the payloads of every version it handles, e.g. a union of them.
 */
export function defineEvent<P>(name: string): EventDefinition<P> {
  return Object.freeze({ name }) as EventDefinition<P>;
}

/** What `onEvent` receives besides the payload: where the event comes from and its version. */
export interface EventMeta {
  /** The topic the event was published to. */
  readonly topic: string;
  /** The event's rev on its topic. */
  readonly rev: number;
  /** The rev the client held for the topic before it applied the event. */
  readonly prev: number;
  /**
   * The BE event's `event_version`, the version of the payload's shape: an integer of at least 1.
   * The push's `:version` header, or the poll answer entry's `version`.
   */
  readonly version: number;
  /** The scope key of a scoped viewset topic, as the BE resolved it for this client; absent otherwise. */
  readonly scope?: string;
}

/**
 * What a subscription does with what arrives on its topic. The handlers are function-typed properties,
 * so a handler whose parameter type is not the definition's payload type, even a wider one, does not compile.
 */
export interface EventHandlers<P> {
  /**
   * Called with the payload and the `EventMeta` of every event of the topic with the subscription's
   * event name, whatever its version, in rev order. `meta` is frozen.
   */
  onEvent: (payload: P, meta: EventMeta) => void;
  /** Called when the client cannot tell which events of the topic it missed: re-read the displayed state. */
  onResync: () => void;
  /** Called with the error of a refused `subscribe` or `poll` of the topic; the subscription is removed. */
  onError?: (error: unknown) => void;
}

/** One subscription to one event name of one topic. */
export interface EventSubscription {
  /**
   * Resolves once the topic is followed: subscribed on the peer's socket, or answered by a poll.
   * Rejects with the error of a refusal that comes first, with the subscription removed: a
   * `ViewSetRequestError` for a status over the peer and for a topic error, the `AxiosError` for a
   * status over axios. Rejects with `Error('The event client is closed')` when `close()` comes first.
   */
  readonly ready: Promise<void>;
  /**
   * Removes the subscription. The last one on a topic ends the client's following of it: over an
   * open peer with an `unsubscribe` call, otherwise by leaving the topic out of the next poll.
   */
  unsubscribe(): void;
  /** Called with the error of every refusal of the topic; initially `EventHandlers.onError`. */
  onError?: (error: unknown) => void;
}

/** The axios method the client uses; an `AxiosInstance` is one. */
export interface EventAxiosLike {
  post<R = unknown>(url: string, data?: unknown, config?: { headers?: Record<string, string> }): Promise<{ data: R }>;
}

/** A stream the remote opened, as `handleEventStream` and `attachEvents` read it. */
export interface EventStreamLike {
  readonly headers: Record<string, unknown>;
  /** Resets the stream; `attachEvents` resets a stream it has no handler for with `REFUSED` (4). */
  reset?(code: number, reason?: string): Promise<void>;
}

/** The handler a muxws peer runs for a stream the remote opened. */
export type EventStreamHandler = (payload: any, stream: EventStreamLike) => void | Promise<void>;

/** A muxws peer as the event client uses it. `onStream` is needed by `attachEvents` only. */
export interface EventPeerLike extends MuxwsPeerLike {
  onStream?(handler: EventStreamHandler): void;
}

/**
 * Where the client's peer comes from: a peer, or a function that returns one. The client calls the
 * function on its first subscription, and again on the ladder's next rung after the peer it returned
 * has closed for good (`onClose` with `willRetry: false`), threw, or rejected; until a peer is
 * adopted the client polls over axios.
 */
export type EventPeerSource = EventPeerLike | (() => EventPeerLike | Promise<EventPeerLike>);

export interface EventClientOptions {
  /** The path the BE's `register_events` serves, e.g. `/events`. */
  basePath: string;
  /**
   * Carries pushes and, while open, the client's calls. A function is called for a peer when the
   * client needs one; see `EventPeerSource`.
   */
  peer?: EventPeerSource;
  /** Carries the client's polls while there is no open peer. */
  axios?: EventAxiosLike;
  /** The ladder's constants. Default `DEFAULT_POLL_SETTINGS`. */
  pollSettings?: PollSettings;
  /** The ladder's jitter draw. Default `Math.random`. */
  draw?: Draw;
  /**
   * Milliseconds between the polls of the subscribed topics while the peer is open and the ladder is
   * held, jittered by `pollSettings.jitterFraction`. `0`, a negative and a non-finite value send
   * none. Default `DEFAULT_CHECK_INTERVAL_MS`.
   */
  checkIntervalMs?: number;
  /**
   * Headers added to every `subscribe`, `unsubscribe` and `poll` call, read per call: as stream
   * headers over the peer, as request headers over axios. A function is called once per call. The BE
   * resolves the scope of a scoped topic from the calling request, and these headers are part of it.
   */
  headers?: Record<string, string> | (() => Record<string, string>);
}

/** 60 s: the default `EventClientOptions.checkIntervalMs`. */
export const DEFAULT_CHECK_INTERVAL_MS = 60000;

export interface EventClient {
  readonly basePath: string;
  /**
   * Subscribes `handlers` to the events named `event.name` on `topic`. The client holds one server
   * subscription and one rev per topic, however many subscriptions it carries.
   */
  subscribe<P>(topic: string, event: EventDefinition<P>, handlers: EventHandlers<NoInfer<P>>): EventSubscription;
  /**
   * Stops the client: clears its timer, ignores every later answer and push, sends `unsubscribe`
   * for every topic subscribed on an open peer, and for every topic whose `subscribe` is in flight
   * once it is answered, rejects every `ready` that has not settled, and unregisters the client from
   * its peer, which may then be given a new client.
   */
  close(): void;
  /**
   * Subscribes `topic`, or every followed topic, again so that the BE resolves its scope anew: over an
   * open peer with a `subscribe` call, otherwise with a poll at once. Until the answer the topic's
   * pushes are held; an answer under another scope key resyncs the topic and applies the held pushes
   * of the new key. A topic whose first answer is pending is refreshed once that answer arrives.
   */
  refresh?(topic?: string): void;
}

// ---------------------------------------------------------------------------
// Wire shapes
// ---------------------------------------------------------------------------

/** One event of a poll answer. */
interface WireEvent {
  rev: number;
  prev: number;
  event: string;
  version: number;
  payload: unknown;
}

/** One topic of a poll answer. */
interface WireTopicAnswer {
  rev: number | null;
  events: WireEvent[] | null;
  /** The `ApiErrorBody` of the topic's refusal. */
  error?: unknown;
  /** The HTTP status of the refusal. */
  status?: number;
  /** The scope key of a scoped topic. */
  scope?: string;
}

interface WirePollAnswer {
  topics?: Record<string, WireTopicAnswer>;
  poll_after_ms?: number | null;
}

interface WireSubscribeAnswer {
  rev: number;
  scope?: string;
}

/** The answer of one call, with the key of the headers it was sent with; see `headersKeyOf`. */
interface CallAnswer<R> {
  data: R;
  headersKey: string;
}

/** `RESET_REFUSED` is muxws' `ResetCode.REFUSED`; muxws is not imported here. */
const RESET_REFUSED = 4;

/** The status `error` carries in `response.status`, the shape of both AxiosError and ViewSetRequestError. */
function statusOf(error: unknown): number | undefined {
  const status = (error as { response?: { status?: unknown } } | null)?.response?.status;
  return typeof status === 'number' ? status : undefined;
}

/**
 * A status that does not refuse a topic: 429, or 500 and above. The call is sent again on the next rung
 * (a subscribe also on the next reconnect), with the topic polled meanwhile.
 */
function isTransientStatus(status: number): boolean {
  return status === 429 || status >= 500;
}

/** A failure with a status from 400 to 499, other than 429. */
function isRefusal(error: unknown): boolean {
  const status = statusOf(error);
  return status !== undefined && status >= 400 && !isTransientStatus(status);
}

/** A rev from a push header: a number, or a string of one. `null` for anything else. */
function revOf(value: unknown): number | null {
  const rev = typeof value === 'string' && value.trim() !== '' ? Number(value) : value;
  return typeof rev === 'number' && Number.isFinite(rev) ? rev : null;
}

/** A version from a push header or a poll entry: an integer of at least 1, or a string of one; `null` otherwise. */
function versionOf(value: unknown): number | null {
  const version = revOf(value);
  return version !== null && Number.isInteger(version) && version >= 1 ? version : null;
}

/**
 * Rethrows `error` in a microtask, where it reaches the environment's uncaught-error reporting
 * instead of the code that called the application handler that threw it.
 */
export function rethrowLater(error: unknown): void {
  globalThis.queueMicrotask(() => {
    throw error;
  });
}

/** Runs an application handler; what it throws is rethrown by `rethrowLater`. */
function runHandler(handler: () => void): void {
  try {
    handler();
  } catch (error) {
    rethrowLater(error);
  }
}

// ---------------------------------------------------------------------------
// The client
// ---------------------------------------------------------------------------

/** One event as it arrived: by push, or in a poll answer. */
interface Arrival {
  rev: number;
  prev: number;
  event: string;
  version: number;
  payload: unknown;
  /** The push's `:scope`; for a poll answer entry, the answer's scope. */
  scope: string | null;
}

class Subscription implements EventSubscription {
  readonly ready: Promise<void>;

  onError?: (error: unknown) => void;

  active = true;

  settled = false;

  private resolveReady!: () => void;

  private rejectReady!: (error: unknown) => void;

  constructor(
    readonly topic: TopicState,
    readonly event: string,
    readonly handlers: EventHandlers<any>,
    private readonly client: EventClientImpl,
  ) {
    this.onError = handlers.onError?.bind(handlers);
    this.ready = new Promise<void>((resolve, reject) => {
      this.resolveReady = resolve;
      this.rejectReady = reject;
    });
    // A refusal rejects `ready` whether or not the application awaits it; a rejection nobody awaits
    // is not reported as unhandled.
    this.ready.catch(() => {});
  }

  unsubscribe(): void {
    this.client.remove(this);
  }

  resolve(): void {
    if (this.settled) return;
    this.settled = true;
    this.resolveReady();
  }

  refuse(error: unknown): void {
    this.active = false;
    if (!this.settled) {
      this.settled = true;
      this.rejectReady(error);
    }
    const onError = this.onError;
    if (onError) runHandler(() => onError(error));
  }

  /** Ends the subscription for `close()`: rejects a `ready` that has not settled, and calls no handler. */
  end(): void {
    this.active = false;
    if (this.settled) return;
    this.settled = true;
    this.rejectReady(new Error('The event client is closed'));
  }
}

/** What the client holds per topic. */
class TopicState {
  /** The rev of the last event applied, or of the first answer; `null` before that answer. */
  rev: number | null = null;

  readonly subs = new Set<Subscription>();

  /** The topic's subscription on the peer's current socket. */
  socket: 'none' | 'subscribing' | 'subscribed' = 'none';

  /** No poll has carried the topic yet. */
  unpolled = true;

  /** A refresh without an open peer waits for the next poll to carry the topic. */
  refreshUnpolled = false;

  /** Pushes that arrived before the first answer gave the topic a rev. */
  pending: Arrival[] = [];

  /** A gap poll of the topic is in flight. */
  gapInFlight = false;

  /** The largest rev a gap push named while a gap poll was in flight. */
  gapRev: number | null = null;

  /**
   * A gap or check poll failed without refusing the topic, or did not answer it: the held rev may be
   * below the BE's. The ladder runs, also over an open peer, until a poll answers the topic.
   */
  behind = false;

  /** The scope key of the topic's last answer; `null` for an unscoped topic and before the first answer. */
  scope: string | null = null;

  /** A `refresh()` of the topic waits for its answer; pushes are held in `pending` meanwhile. */
  refreshing = false;

  /** `refresh()` was called before the topic's first answer; the topic is refreshed once it arrives. */
  refreshOnAnswer = false;

  /** The headers key of the call whose answer the topic last applied; `null` before that answer. */
  sentHeaders: string | null = null;

  constructor(readonly topic: string) {}
}

class EventClientImpl implements EventClient {
  readonly basePath: string;

  /** The adopted peer: the bare `peer` option, or the last peer the source returned until it closed for good. */
  private peer?: EventPeerLike;

  /** The `peer` option when it is a function. */
  private readonly peerSource?: () => EventPeerLike | Promise<EventPeerLike>;

  /** A call of `peerSource` has not settled. */
  private peerRequested = false;

  private readonly http?: EventAxiosLike;

  private readonly headersOption?: Record<string, string> | (() => Record<string, string>);

  private readonly ladder: PollLadder;

  private readonly topics = new Map<string, TopicState>();

  /** Whether the peer is open, for a peer that does not report `isOpen`. */
  private trackedOpen = true;

  /** Incremented on every close and reconnect of the peer; an answer from an older socket is dropped. */
  private socketGeneration = 0;

  private loop: 'idle' | 'waiting' | 'polling' = 'idle';

  private timer: ReturnType<typeof setTimeout> | null = null;

  private readonly checkIntervalMs: number;

  private readonly pollSettings: PollSettings;

  private readonly draw: Draw;

  /** The timer of the next check poll; see `check`. */
  private checkTimer: ReturnType<typeof setTimeout> | null = null;

  private closed = false;

  /** The peer closed while a rung was polling; that rung schedules the base rung next. */
  private resumeOnBase = false;

  constructor(options: EventClientOptions) {
    this.basePath = options.basePath.replace(/\/$/, '');
    this.http = options.axios;
    this.headersOption = options.headers;
    this.pollSettings = options.pollSettings ?? DEFAULT_POLL_SETTINGS;
    this.draw = options.draw ?? Math.random;
    this.ladder = new PollLadder(this.pollSettings, this.draw);
    this.checkIntervalMs = options.checkIntervalMs ?? DEFAULT_CHECK_INTERVAL_MS;
    if (typeof options.peer === 'function') this.peerSource = options.peer;
    else if (options.peer) this.watchPeer(options.peer);
  }

  // -- subscriptions --------------------------------------------------------

  subscribe<P>(topic: string, event: EventDefinition<P>, handlers: EventHandlers<NoInfer<P>>): EventSubscription {
    if (this.closed) throw new Error('The event client is closed');
    let state = this.topics.get(topic);
    if (!state) {
      state = new TopicState(topic);
      this.topics.set(topic, state);
      if (this.peerOpen()) this.sendSubscribe(state);
      else this.requestPeer();
    }
    const sub = new Subscription(state, event.name, handlers, this);
    state.subs.add(sub);
    if (state.rev !== null) sub.resolve();
    this.ensureLoop();
    this.scheduleCheck();
    return sub;
  }

  remove(sub: Subscription): void {
    if (!sub.active) return;
    sub.active = false;
    const state = sub.topic;
    state.subs.delete(sub);
    if (state.subs.size > 0 || this.topics.get(state.topic) !== state) return;
    this.topics.delete(state.topic);
    // A subscribe still in flight sends the unsubscribe when it is answered; see sendSubscribe.
    if (state.socket === 'subscribed') this.sendUnsubscribe(state.topic);
    this.ensureLoop();
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.clearTimer();
    this.clearCheckTimer();
    this.loop = 'idle';
    // A subscribe in flight sends the unsubscribe when it is answered; see sendSubscribe.
    for (const state of this.topics.values()) {
      if (state.socket === 'subscribed') this.sendUnsubscribe(state.topic);
      for (const sub of state.subs) sub.end();
    }
    this.topics.clear();
    if (this.peer && registry.get(this.peer) === this) registry.delete(this.peer);
  }

  refresh(topic?: string): void {
    if (this.closed) return;
    for (const state of [...this.topics.values()]) {
      if (topic === undefined || state.topic === topic) this.refreshState(state);
    }
  }

  /**
   * Refreshes one answered topic: holds its pushes until the answer of a `subscribe` over the open
   * peer, or of a poll at once over axios.
   */
  private refreshState(state: TopicState): void {
    if (this.topics.get(state.topic) !== state) return;
    if (state.rev === null) {
      state.refreshOnAnswer = true;
      return;
    }
    state.refreshing = true;
    if (this.peerOpen()) {
      this.sendSubscribe(state);
      return;
    }
    state.behind = true;
    state.refreshUnpolled = true;
    if (this.http && this.loop !== 'polling') this.schedule(0);
  }

  /** Removes the topic and reports `error` to every subscription on it. */
  private refuse(state: TopicState, error: unknown): void {
    if (this.topics.get(state.topic) !== state) return;
    this.topics.delete(state.topic);
    if (state.socket === 'subscribed') this.sendUnsubscribe(state.topic);
    for (const sub of [...state.subs]) sub.refuse(error);
    state.subs.clear();
    this.ensureLoop();
  }

  // -- the rev rules ---------------------------------------------------------

  /**
   * Sets the first rev and scope of a topic, replays the pushes held for it under that scope and
   * resolves its subscriptions. The topic is then refreshed when `refresh()` was called before the
   * answer, or when it is scoped and `headers()` changed since the answer's call was sent.
   */
  private follow(state: TopicState, rev: number, scope: string | null): void {
    state.rev = rev;
    state.scope = scope;
    state.refreshing = false;
    const pending = state.pending.filter((arrival) => arrival.scope === scope);
    state.pending = [];
    for (const sub of state.subs) sub.resolve();
    for (const arrival of pending) {
      // A handler of a replayed push may have ended the following of the topic.
      if (this.topics.get(state.topic) !== state) return;
      this.receive(state, arrival);
    }
    // A replayed push may have started a refresh already.
    const headersChanged = scope !== null && this.currentHeadersKey() !== state.sentHeaders;
    const refresh = state.refreshOnAnswer || headersChanged;
    state.refreshOnAnswer = false;
    if (refresh && !state.refreshing) this.refreshState(state);
  }

  /**
   * Applies `arrival` when its `prev` is the held rev, ignores it when its rev is at most the held
   * one, and reports a gap otherwise.
   */
  private apply(state: TopicState, arrival: Arrival): 'applied' | 'ignored' | 'gap' {
    const held = state.rev as number;
    if (arrival.prev === held) {
      state.rev = arrival.rev;
      const meta: EventMeta = Object.freeze({
        topic: state.topic,
        rev: arrival.rev,
        prev: arrival.prev,
        version: arrival.version,
        ...(state.scope !== null ? { scope: state.scope } : {}),
      });
      for (const sub of [...state.subs]) {
        if (sub.active && sub.event === arrival.event) runHandler(() => sub.handlers.onEvent(arrival.payload, meta));
      }
      return 'applied';
    }
    return arrival.rev <= held ? 'ignored' : 'gap';
  }

  /**
   * A pushed event: held until the topic has a rev and while it is refreshing, then applied, ignored or
   * answered with a gap poll. A push of a scoped topic arriving after `headers()` changed since the
   * topic was answered refreshes the topic and is held; a push under another scope key is ignored.
   */
  private receive(state: TopicState, arrival: Arrival): void {
    if (state.rev === null || state.refreshing) {
      state.pending.push(arrival);
      return;
    }
    if (state.scope !== null && state.sentHeaders !== null && this.currentHeadersKey() !== state.sentHeaders) {
      this.refreshState(state);
      state.pending.push(arrival);
      return;
    }
    if (arrival.scope !== state.scope) return;
    if (this.apply(state, arrival) === 'gap') this.gapPoll(state, arrival.rev);
  }

  /**
   * A later answer of `state` under another scope key: the topic resyncs to `rev`, and the held pushes
   * of the new key above `rev` are applied after it. A scope change seen by a poll subscribes the topic
   * again on an open socket, so that the BE's subscription follows the new key.
   */
  private scopeChange(state: TopicState, scope: string | null, rev: number, viaPoll: boolean): void {
    const held = state.pending.filter((arrival) => arrival.scope === scope && arrival.rev > rev);
    held.sort((a, b) => a.rev - b.rev);
    state.scope = scope;
    state.refreshing = false;
    this.resync(state, rev);
    for (const arrival of held) {
      if (this.topics.get(state.topic) !== state) return;
      this.receive(state, arrival);
    }
    if (viaPoll && state.socket === 'subscribed' && this.topics.get(state.topic) === state) this.sendSubscribe(state);
  }

  /** Ends a refresh answered under the same scope key: the held pushes are received again. */
  private endRefresh(state: TopicState): void {
    if (!state.refreshing) return;
    state.refreshing = false;
    const held = state.pending;
    state.pending = [];
    for (const arrival of held) {
      if (this.topics.get(state.topic) !== state) return;
      this.receive(state, arrival);
    }
  }

  /** Calls `onResync` on every subscription of the topic; the held rev becomes `rev`. */
  private resync(state: TopicState, rev: number): void {
    state.rev = rev;
    state.pending = [];
    for (const sub of [...state.subs]) {
      if (sub.active) runHandler(() => sub.handlers.onResync());
    }
  }

  /**
   * One topic of a poll answer; `cursor` is the rev the poll carried (pushes may have moved the held
   * rev since). The rules are tabled in docs/api/vue-events.md, "Push, gap, poll and resync". Returns
   * `'unanswered'` for a non-refusing error and for an answer without a rev.
   */
  private applyTopicAnswer(
    state: TopicState,
    cursor: number | null,
    answer: WireTopicAnswer,
    headersKey: string,
  ): 'moved' | 'answered' | 'unanswered' {
    if (answer.error !== undefined && answer.error !== null) {
      if (!isApiErrorBody(answer.error) || typeof answer.status !== 'number') return 'unanswered';
      if (isTransientStatus(answer.status)) return 'unanswered';
      this.refuse(state, new ViewSetRequestError(answer.status, answer.error));
      return 'answered';
    }
    const rev = revOf(answer.rev);
    if (rev === null) return 'unanswered';
    state.behind = false;
    state.sentHeaders = headersKey;
    const scope = typeof answer.scope === 'string' ? answer.scope : null;
    if (state.rev === null) {
      this.follow(state, rev, scope);
      return 'answered';
    }
    if (scope !== state.scope) {
      this.scopeChange(state, scope, rev, true);
      return 'moved';
    }
    const outcome = this.applyEvents(state, cursor, rev, answer);
    this.endRefresh(state);
    return outcome;
  }

  /**
   * The events of a poll answer under the held scope key. `events: null` is no resync when pushes
   * already moved the held rev to the answer's. An event with `prev` below the held rev and rev above
   * it is applied: the BE chains an answer's events.
   */
  private applyEvents(
    state: TopicState,
    cursor: number | null,
    rev: number,
    answer: WireTopicAnswer,
  ): 'moved' | 'answered' {
    const held = state.rev as number;
    if (!Array.isArray(answer.events)) {
      if (held !== cursor && rev <= held) return 'answered';
      this.resync(state, rev);
      return rev !== held ? 'moved' : 'answered';
    }
    if (cursor !== null && rev < cursor) {
      this.resync(state, rev);
      return 'moved';
    }
    for (const event of answer.events) {
      const arrival = toArrival(event, state.scope);
      if (arrival === null || this.apply(state, chained(arrival, state.rev as number)) === 'gap') {
        this.resync(state, rev);
        return 'moved';
      }
    }
    // The answer covers every event up to its rev; the ones it leaves out were not for this client.
    if (rev > (state.rev as number)) state.rev = rev;
    return state.rev !== held ? 'moved' : 'answered';
  }

  /**
   * Receives a pushed event; see `handleEventStream`. A push without a string `:topic` and `:event`,
   * or without a usable `:rev`, `:prev` or `:version`, is ignored. A string `:scope` is the push's
   * scope key.
   */
  push(headers: Record<string, unknown>, payload: unknown): void {
    const topic = headers[':topic'];
    const event = headers[':event'];
    const rev = revOf(headers[':rev']);
    const prev = revOf(headers[':prev']);
    const version = versionOf(headers[':version']);
    if (typeof topic !== 'string' || typeof event !== 'string' || rev === null || prev === null || version === null) {
      return;
    }
    const state = this.topics.get(topic);
    if (!state) return;
    const scope = typeof headers[':scope'] === 'string' ? headers[':scope'] : null;
    this.receive(state, { rev, prev, event, version, payload, scope });
  }

  // -- calls -------------------------------------------------------------------

  private peerOpen(): boolean {
    if (!this.peer) return false;
    return this.peer.isOpen ?? this.trackedOpen;
  }

  /** `EventClientOptions.headers`, read now. */
  private resolveHeaders(): Record<string, string> {
    const option = this.headersOption;
    return (typeof option === 'function' ? option() : option) ?? {};
  }

  /** The headers key of `EventClientOptions.headers` now; `null` when reading them throws. */
  private currentHeadersKey(): string | null {
    try {
      return headersKeyOf(this.resolveHeaders());
    } catch {
      return null;
    }
  }

  /**
   * One call to `{basePath}/{name}`: one muxws stream while the peer is open, one axios POST
   * otherwise, with `EventClientOptions.headers` read once for the call. `subscribe` and
   * `unsubscribe` exist on the socket only and are refused without one.
   */
  private async call<R>(name: 'subscribe' | 'unsubscribe' | 'poll', body: unknown): Promise<CallAnswer<R>> {
    const path = `${this.basePath}/${name}`;
    const headers = this.resolveHeaders();
    const headersKey = headersKeyOf(headers);
    if (this.peer && this.peerOpen()) {
      const stream = this.peer.open(body, { headers: { ...headers, ':method': 'POST', ':path': path }, end: true });
      const result = stream.result();
      await Promise.race([stream.replyHeadersArrived, result]);
      const status = readStatus(stream.replyHeaders);
      const data = await result;
      if (status >= 400) throw new ViewSetRequestError(status, data, readHeaders(stream.replyHeaders));
      return { data: data as R, headersKey };
    }
    if (name !== 'poll' || !this.http) throw new Error(`The event client has no transport for '${name}'`);
    const response =
      this.headersOption === undefined
        ? await this.http.post<R>(path, body)
        : await this.http.post<R>(path, body, { headers });
    return { data: response.data, headersKey };
  }

  /** The body of a poll of `sent`: the cursors, and the scope key held for every scoped topic. */
  private pollBody(sent: readonly { state: TopicState; cursor: number | null }[]): {
    cursors: Record<string, number | null>;
    scopes: Record<string, string>;
  } {
    const cursors: Record<string, number | null> = {};
    const scopes: Record<string, string> = {};
    for (const { state, cursor } of sent) {
      cursors[state.topic] = cursor;
      if (state.scope !== null) scopes[state.topic] = state.scope;
    }
    return { cursors, scopes };
  }

  /**
   * Runs `op`, a `subscribe` or `unsubscribe` of `topic`, after the previous socket call of `topic`
   * (from any client of the peer) settled: the BE serves a socket's streams concurrently, so an
   * `unsubscribe` answered after a later `subscribe` would end that subscription.
   */
  private onSocket(topic: string, op: () => Promise<void>): void {
    if (!this.peer) return;
    let calls = socketCalls.get(this.peer);
    if (!calls) {
      calls = new Map();
      socketCalls.set(this.peer, calls);
    }
    const inFlight = calls;
    const before = inFlight.get(topic);
    let done: Promise<void> | undefined;
    const run = async (): Promise<void> => {
      if (before) await before;
      try {
        await op();
      } finally {
        if (inFlight.get(topic) === done) inFlight.delete(topic);
      }
    };
    done = run().catch(rethrowLater);
    inFlight.set(topic, done);
  }

  private sendSubscribe(state: TopicState): void {
    const generation = this.socketGeneration;
    state.socket = 'subscribing';
    const topic = state.topic;
    this.onSocket(topic, async () => {
      if (this.closed || generation !== this.socketGeneration || this.topics.get(topic) !== state) return;
      let answer: WireSubscribeAnswer;
      let headersKey: string;
      try {
        ({ data: answer, headersKey } = await this.call<WireSubscribeAnswer>('subscribe', { topic }));
      } catch (error) {
        if (this.closed || generation !== this.socketGeneration || this.topics.get(topic) !== state) return;
        state.socket = 'none';
        if (isRefusal(error)) {
          this.refuse(state, error);
          return;
        }
        if (state.refreshing) {
          state.refreshing = false;
          state.pending = [];
          state.behind = true;
        }
        this.ensureLoop();
        return;
      }
      if (generation !== this.socketGeneration) return;
      if (this.closed || this.topics.get(topic) !== state) {
        // The socket's subscription is per topic: a topic this client subscribed again since keeps it.
        if (this.closed || !this.topics.has(topic)) await this.unsubscribeCall(topic);
        return;
      }
      state.socket = 'subscribed';
      const rev = revOf(answer?.rev);
      if (rev !== null) {
        state.sentHeaders = headersKey;
        const scope = typeof answer?.scope === 'string' ? answer.scope : null;
        if (state.rev === null) this.follow(state, rev, scope);
        else if (scope !== state.scope) this.scopeChange(state, scope, rev, false);
        else {
          if (rev > state.rev) this.gapPoll(state, rev);
          else state.behind = false;
          this.endRefresh(state);
        }
      }
      this.ensureLoop();
    });
  }

  private sendUnsubscribe(topic: string): void {
    const generation = this.socketGeneration;
    this.onSocket(topic, async () => {
      if (generation === this.socketGeneration) await this.unsubscribeCall(topic);
    });
  }

  /** One `unsubscribe` call over the open peer; its failure is not reported. */
  private async unsubscribeCall(topic: string): Promise<void> {
    if (!this.peerOpen()) return;
    try {
      await this.call<unknown>('unsubscribe', { topic });
    } catch {
      // The socket's subscription ends with the socket at the latest.
    }
  }

  /**
   * One poll of `state`'s topic from its held rev, for a gap. A gap that arrives while one is in
   * flight is polled again after it, when the held rev has not reached that gap's rev by then. A gap
   * poll that fails without refusing the topic, or whose answer does not answer the topic, marks the
   * topic `behind`, and the ladder polls it.
   */
  private gapPoll(state: TopicState, rev: number): void {
    if (this.closed || this.topics.get(state.topic) !== state) return;
    if (state.gapInFlight) {
      state.gapRev = Math.max(state.gapRev ?? rev, rev);
      return;
    }
    if (!this.peerOpen() && !this.http) return;
    state.gapInFlight = true;
    const cursor = state.rev;
    void this.call<WirePollAnswer>('poll', this.pollBody([{ state, cursor }])).then(
      ({ data: answer, headersKey }) => {
        state.gapInFlight = false;
        if (this.closed || this.topics.get(state.topic) !== state) return;
        const entry = answer?.topics?.[state.topic];
        if (entry && this.applyTopicAnswer(state, cursor, entry, headersKey) !== 'unanswered') this.gapAgain(state);
        else this.fallBehind(state);
      },
      (error: unknown) => {
        state.gapInFlight = false;
        if (this.closed || this.topics.get(state.topic) !== state) return;
        if (isRefusal(error)) this.refuse(state, error);
        else this.fallBehind(state);
      },
    );
  }

  private gapAgain(state: TopicState): void {
    const wanted = state.gapRev;
    state.gapRev = null;
    if (wanted !== null && state.rev !== null && state.rev < wanted && this.topics.get(state.topic) === state) {
      this.gapPoll(state, wanted);
    }
  }

  /** Leaves the gap of `state` to the ladder; see `TopicState.behind`. */
  private fallBehind(state: TopicState): void {
    state.gapRev = null;
    state.behind = true;
    this.ensureLoop();
  }

  // -- the ladder ----------------------------------------------------------------

  /**
   * Whether the ladder runs: there is a topic, and either the peer is open and some topic is not
   * subscribed on its socket or is `behind`, or the peer is not open and there is axios to poll over
   * or a peer source to call.
   */
  private shouldPoll(): boolean {
    if (this.closed || this.topics.size === 0) return false;
    if (this.peerOpen()) {
      return [...this.topics.values()].some((state) => state.socket !== 'subscribed' || state.behind);
    }
    return this.http !== undefined || (this.peerSource !== undefined && this.peer === undefined);
  }

  /**
   * A topic without a rev that no poll has carried, and a topic refreshed since the last poll, are
   * polled at once over axios when there is no open peer to subscribe them on.
   */
  private pollNow(): boolean {
    return (
      this.http !== undefined &&
      !this.peerOpen() &&
      [...this.topics.values()].some((state) => (state.rev === null && state.unpolled) || state.refreshUnpolled)
    );
  }

  /** Starts, holds or hurries the ladder to match the state; stops the check timer without a topic. */
  private ensureLoop(): void {
    if (this.closed) return;
    if (this.topics.size === 0) this.clearCheckTimer();
    if (!this.shouldPoll()) {
      if (this.loop === 'waiting') {
        this.clearTimer();
        this.loop = 'idle';
      }
      return;
    }
    if (this.loop === 'idle') this.schedule(this.pollNow() ? 0 : this.ladder.resume());
    else if (this.loop === 'waiting' && this.pollNow()) this.schedule(0);
  }

  private schedule(delay: number): void {
    this.clearTimer();
    this.loop = 'waiting';
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.rung();
    }, delay);
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  /**
   * One rung: a call of the peer source when the client has no peer, one poll of every followed
   * topic, then the next rung's delay from the ladder. Without an open peer and without axios the
   * rung polls nothing.
   */
  private async rung(): Promise<void> {
    if (!this.shouldPoll()) {
      this.loop = 'idle';
      return;
    }
    this.requestPeer();
    if (!this.peerOpen() && !this.http) {
      this.schedule(this.ladder.next({ anyActive: true, anyDialogOpen: false, revChanged: false, pollAfterMs: null }));
      return;
    }
    this.loop = 'polling';
    const sent = [...this.topics.values()].map((state) => ({ state, cursor: state.rev }));
    for (const { state } of sent) {
      state.unpolled = false;
      state.refreshUnpolled = false;
    }

    let revChanged = false;
    let pollAfterMs: number | null = null;
    try {
      const polled = this.call<WirePollAnswer>('poll', this.pollBody(sent));
      this.resubscribe();
      const { data: answer, headersKey } = await polled;
      if (this.closed) return;
      pollAfterMs = typeof answer?.poll_after_ms === 'number' ? answer.poll_after_ms : null;
      for (const { state, cursor } of sent) {
        if (this.topics.get(state.topic) !== state) continue;
        const entry = answer?.topics?.[state.topic];
        if (entry && this.applyTopicAnswer(state, cursor, entry, headersKey) === 'moved') revChanged = true;
      }
    } catch (error) {
      if (this.closed) return;
      if (isRefusal(error)) for (const { state } of sent) this.refuse(state, error);
    }

    this.loop = 'idle';
    const resume = this.resumeOnBase;
    this.resumeOnBase = false;
    if (!this.shouldPoll()) return;
    let delay: number;
    if (this.pollNow()) delay = 0;
    else if (resume) delay = this.ladder.resume();
    // `anyActive: true`: the client cannot tell whether anything on a topic is active.
    else delay = this.ladder.next({ anyActive: true, anyDialogOpen: false, revChanged, pollAfterMs });
    this.schedule(delay);
  }

  /**
   * Sends `subscribe` again, over the open peer, for every topic not subscribed on its socket and
   * without a subscribe in flight: a topic whose subscribe failed with a 429, a status of 500 or
   * above, or without a status. The ladder calls it on every rung, so the retries follow its delays.
   */
  private resubscribe(): void {
    if (!this.peerOpen()) return;
    for (const state of this.topics.values()) if (state.socket === 'none') this.sendSubscribe(state);
  }

  // -- the check poll ----------------------------------------------------------------

  /** Starts the check timer unless it runs, checks are off, or the client has no peer or no topic. */
  private scheduleCheck(): void {
    if (this.closed || this.checkTimer !== null || !this.peer) return;
    if (!Number.isFinite(this.checkIntervalMs) || this.checkIntervalMs <= 0) return;
    if (this.topics.size === 0) return;
    const delay = Math.round(this.checkIntervalMs * jitterMultiplier(this.draw, this.pollSettings));
    this.checkTimer = setTimeout(() => {
      this.checkTimer = null;
      void this.check();
    }, delay);
  }

  private clearCheckTimer(): void {
    if (this.checkTimer !== null) {
      clearTimeout(this.checkTimer);
      this.checkTimer = null;
    }
  }

  /**
   * The check poll: while the ladder is idle, one poll over the open peer of every subscribed topic
   * that has a rev, is not `behind` and has no gap poll in flight. It catches a lost push of a topic's
   * last event. A topic it does not answer falls behind to the ladder.
   */
  private async check(): Promise<void> {
    if (this.closed) return;
    const sent = [...this.topics.values()]
      .filter((state) => state.socket === 'subscribed' && state.rev !== null && !state.behind && !state.gapInFlight)
      .map((state) => ({ state, cursor: state.rev }));
    if (this.loop !== 'idle' || !this.peerOpen() || sent.length === 0) {
      this.scheduleCheck();
      return;
    }
    const generation = this.socketGeneration;
    try {
      const { data: answer, headersKey } = await this.call<WirePollAnswer>('poll', this.pollBody(sent));
      if (this.closed) return;
      for (const { state, cursor } of sent) {
        if (this.topics.get(state.topic) !== state) continue;
        const entry = answer?.topics?.[state.topic];
        if (!entry || this.applyTopicAnswer(state, cursor, entry, headersKey) === 'unanswered') {
          this.checkBehind(state, generation);
        }
      }
    } catch (error) {
      if (this.closed) return;
      for (const { state } of sent) {
        if (this.topics.get(state.topic) !== state) continue;
        if (isRefusal(error)) this.refuse(state, error);
        else this.checkBehind(state, generation);
      }
    }
    this.ensureLoop();
    this.scheduleCheck();
  }

  /** Marks `state` `behind` after a check poll, unless the socket it was sent on has been replaced. */
  private checkBehind(state: TopicState, generation: number): void {
    if (generation === this.socketGeneration) state.behind = true;
  }

  // -- the peer --------------------------------------------------------------------

  /**
   * Calls the peer source when the client has none and no call is in flight. A peer it returns is
   * adopted; a call that throws or rejects leaves the client without a peer until the next rung.
   */
  private requestPeer(): void {
    const source = this.peerSource;
    if (!source || this.peer || this.peerRequested || this.closed) return;
    let result: EventPeerLike | Promise<EventPeerLike>;
    try {
      result = source();
    } catch {
      return;
    }
    this.peerRequested = true;
    Promise.resolve(result).then(
      (peer) => {
        this.peerRequested = false;
        if (this.closed || this.peer) return;
        this.adoptPeer(peer);
      },
      () => {
        this.peerRequested = false;
      },
    );
  }

  /**
   * Registers the client for `peer` and subscribes every followed topic on its socket. A peer whose
   * `isClosed` is true is not adopted, and the next rung calls the source again; a peer with another
   * client is not adopted, and the error is rethrown outside the client.
   */
  private adoptPeer(peer: EventPeerLike): void {
    if (peer.isClosed === true) return;
    const other = registry.get(peer);
    if (other && other !== this) {
      rethrowLater(new Error('This peer already has an event client'));
      return;
    }
    registry.set(peer, this);
    this.watchPeer(peer);
    this.trackedOpen = peer.isOpen ?? true;
    this.socketGeneration += 1;
    if (this.peerOpen()) for (const state of this.topics.values()) this.sendSubscribe(state);
    this.ensureLoop();
    this.scheduleCheck();
  }

  /**
   * Makes `peer` the client's peer and installs its `onClose` and `onReconnect` handlers; a handler
   * of a peer the client no longer holds does nothing. A peer from the source that closes for good
   * is dropped, and the source is called on the next rung.
   */
  private watchPeer(peer: EventPeerLike): void {
    this.peer = peer;
    peer.onClose?.((reason) => {
      if (this.peer !== peer) return;
      this.onPeerClose();
      if (this.peerSource && reason?.willRetry === false) this.dropPeer(peer);
    });
    peer.onReconnect?.(() => {
      if (this.peer === peer) this.onPeerReconnect();
    });
  }

  /** Unregisters the client from `peer`, which closed for good, and leaves the client without a peer. */
  private dropPeer(peer: EventPeerLike): void {
    if (registry.get(peer) === this) registry.delete(peer);
    socketCalls.get(peer)?.clear();
    this.peer = undefined;
    this.clearCheckTimer();
    this.ensureLoop();
  }

  /** Every socket subscription is gone; the ladder resumes on its base rung. */
  private onPeerClose(): void {
    // A closed client keeps tracking the socket, for the `unsubscribe` of a subscribe in flight.
    this.trackedOpen = false;
    this.socketGeneration += 1;
    if (this.closed) return;
    if (this.peer) socketCalls.get(this.peer)?.clear();
    for (const state of this.topics.values()) state.socket = 'none';
    if (this.loop === 'waiting') {
      this.clearTimer();
      this.loop = 'idle';
    }
    if (this.loop === 'polling') this.resumeOnBase = true;
    this.ensureLoop();
  }

  /** Every topic is subscribed on the new socket; the ladder is held once all of them are. */
  private onPeerReconnect(): void {
    this.trackedOpen = true;
    this.socketGeneration += 1;
    if (this.closed) return;
    if (this.peer) socketCalls.get(this.peer)?.clear();
    for (const state of this.topics.values()) this.sendSubscribe(state);
    this.ensureLoop();
  }
}

/** `arrival` with its `prev` raised to `held` when its `prev` is below `held` and its rev above it. */
function chained(arrival: Arrival, held: number): Arrival {
  return arrival.prev < held && arrival.rev > held ? { ...arrival, prev: held } : arrival;
}

/**
 * A poll answer entry as an `Arrival` under `scope`; `null` for an entry without a usable rev, prev,
 * event name or version.
 */
function toArrival(event: WireEvent, scope: string | null): Arrival | null {
  const rev = revOf(event?.rev);
  const prev = revOf(event?.prev);
  const version = versionOf(event?.version);
  if (rev === null || prev === null || version === null || typeof event.event !== 'string') return null;
  return { rev, prev, event: event.event, version, payload: event.payload, scope };
}

/** `headers` as a key that compares equal for the same names (case-insensitively) and values. */
function headersKeyOf(headers: Record<string, string>): string {
  const entries = Object.entries(headers).map(([name, value]) => [name.toLowerCase(), value] as const);
  entries.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return JSON.stringify(entries);
}

// ---------------------------------------------------------------------------
// Registry and stream dispatch
// ---------------------------------------------------------------------------

const registry = new WeakMap<object, EventClientImpl>();

/** The socket calls of every client of a peer in flight, by topic; see `EventClientImpl.onSocket`. */
const socketCalls = new WeakMap<object, Map<string, Promise<void>>>();

/**
 * Builds an event client. Throws without `peer` and without `axios`, and for a `peer` that already
 * has a client; the client is registered for its `peer` until `close()`. A client whose `peer` is a
 * function is registered for each peer it adopts, until that peer closes for good or `close()`.
 */
export function createEventClient(options: EventClientOptions): EventClient {
  const { peer } = options;
  if (!peer && !options.axios) throw new Error('createEventClient needs a peer, axios, or both');
  if (peer && typeof peer !== 'function' && registry.has(peer))
    throw new Error('This peer already has an event client');
  const client = new EventClientImpl(options);
  if (peer && typeof peer !== 'function') registry.set(peer, client);
  return client;
}

/** The client registered for `peer`, or `undefined`. */
export function eventsOf(peer: object): EventClient | undefined {
  return registry.get(peer);
}

/**
 * Dispatches `stream` to `peer`'s client when its headers carry `:event`, and returns `true`;
 * returns `false` and does nothing for any other stream. An event of a topic the client does not
 * follow, and an event on a peer without a client, are ignored.
 *
 * For an application or a library that owns the peer's `onStream` handler and composes this into it.
 */
export function handleEventStream(peer: object, payload: unknown, stream: EventStreamLike): boolean {
  const headers = stream?.headers ?? {};
  if (typeof headers[':event'] !== 'string') return false;
  registry.get(peer)?.push(headers, payload);
  return true;
}

/**
 * Installs `peer`'s `onStream` handler: event streams go to the peer's client, every other stream
 * to `fallback`, or is reset with `REFUSED` without one. A muxws peer has one handler slot, and
 * this takes it.
 */
export function attachEvents(peer: EventPeerLike, fallback?: EventStreamHandler): void {
  if (typeof peer.onStream !== 'function') throw new Error('attachEvents needs a peer with onStream');
  peer.onStream(async (payload, stream) => {
    if (handleEventStream(peer, payload, stream)) return;
    if (fallback) {
      await fallback(payload, stream);
      return;
    }
    await stream.reset?.(RESET_REFUSED, 'no handler for this stream');
  });
}

// ---------------------------------------------------------------------------
// Deferred subscriptions
// ---------------------------------------------------------------------------

/**
 * A subscription made once `client` resolves (`watch()` on a muxws ViewSet whose peer source has not
 * resolved yet). `unsubscribe()` before then subscribes nothing. When `client` rejects or `subscribe`
 * throws, `ready` rejects and the error goes to `onError`, or to `rethrowLater` without one.
 */
export function deferSubscription(
  client: Promise<EventClient>,
  subscribe: (client: EventClient) => EventSubscription,
  onError?: (error: unknown) => void,
): EventSubscription {
  let inner: EventSubscription | null = null;
  let cancelled = false;
  const report = (error: unknown): void => {
    if (cancelled) return;
    const handler = outer.onError;
    if (handler) runHandler(() => handler(error));
    else rethrowLater(error);
  };
  const outer: { -readonly [P in keyof EventSubscription]: EventSubscription[P] } = {
    ready: client.then(
      (resolved) => {
        if (cancelled) return undefined;
        try {
          inner = subscribe(resolved);
        } catch (error) {
          report(error);
          throw error;
        }
        inner.onError = (error) => outer.onError?.(error);
        return inner.ready;
      },
      (error: unknown) => {
        report(error);
        throw error;
      },
    ),
    unsubscribe() {
      cancelled = true;
      inner?.unsubscribe();
    },
    onError,
  };
  outer.ready.catch(() => {});
  return outer;
}
