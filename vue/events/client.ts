/**
 * The event client: follows topics the BE publishes events to, with pushes over a muxws peer and
 * polls over the peer or over axios.
 *
 *   const peer = await connect('wss://example.test/ws');
 *   attachEvents(peer);
 *   const events = createEventClient({ basePath: '/events', peer, axios });
 *
 *   const ImportFinished = defineEvent<{ file: string; rows: number }>('import_finished');
 *   const subscription = events.subscribe('imports:7', ImportFinished, {
 *     onEvent(payload, { version }) { ... },
 *     onResync() { ... },
 *   });
 *   await subscription.ready;
 *
 * The BE writes every event to its store before it pushes it, and a poll answers from that store.
 * Per topic the client holds the rev of the last event it applied and applies an event only when the
 * event's `prev` is that rev. A larger rev with any other `prev` is a gap, answered by one poll of the
 * topic; a poll the BE's log cannot answer is a resync, on which every subscription of the topic
 * re-reads what it displays.
 *
 * Every event carries a version, the BE event's `event_version`. An event is dispatched by topic
 * and event name, whatever its version, and its handler receives the version in `EventMeta`.
 *
 * While the peer is open every topic holds a subscription on its socket and the polling ladder is
 * held. Without an open peer the client polls every topic it follows in one call per rung of the
 * ladder (poll.ts), over axios.
 *
 * A gap is seen only when a later event of the topic arrives, so a lost push of a topic's last event
 * is seen by a poll only. While the peer is open and the ladder is held, the client polls every
 * subscribed topic over the peer once per `checkIntervalMs`.
 *
 * The client's own calls (`subscribe`, `unsubscribe`, `poll`) carry no `Idempotency-Key` and are
 * never repeated by the proxies' repeat machinery: a repeat of `subscribe` answered from a stored
 * outcome would not subscribe the socket the repeat travelled on.
 */

import { ViewSetRequestError } from '../errors';
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
}

/**
 * What a subscription does with what arrives on its topic.
 *
 * The handlers are function-typed properties, so a handler whose parameter type is not the
 * definition's payload type, a wider one included, does not compile.
 */
export interface EventHandlers<P> {
  /**
   * Called with the payload and the `EventMeta` of every event of the topic with the subscription's
   * event name, whatever its version, in rev order. `meta` is frozen.
   */
  onEvent: (payload: P, meta: EventMeta) => void;
  /**
   * Called when the client cannot tell which events of the topic it missed. The subscription re-reads
   * the state it displays.
   */
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
  post<R = unknown>(url: string, data?: unknown): Promise<{ data: R }>;
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

export interface EventClientOptions {
  /** The path the BE's `register_events` serves, e.g. `/events`. */
  basePath: string;
  /** Carries pushes and, while open, the client's calls. */
  peer?: EventPeerLike;
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
  error?: string;
}

interface WirePollAnswer {
  topics?: Record<string, WireTopicAnswer>;
  poll_after_ms?: number | null;
}

interface WireSubscribeAnswer {
  rev: number;
}

/**
 * The status a topic error of a poll answer stands for: the status of the BE error whose `code` the
 * answer carries. A code not listed here stands for 403.
 */
const TOPIC_ERROR_STATUS: Readonly<Record<string, number>> = {
  event_topic_invalid: 422,
  event_topic_refused: 403,
  not_authorized: 403,
  session_expired: 401,
  rate_limited: 429,
};

/** `RESET_REFUSED` is muxws' `ResetCode.REFUSED`; muxws is not imported here. */
const RESET_REFUSED = 4;

/** The status `error` carries in `response.status`, the shape of both AxiosError and ViewSetRequestError. */
function statusOf(error: unknown): number | undefined {
  const status = (error as { response?: { status?: unknown } } | null)?.response?.status;
  return typeof status === 'number' ? status : undefined;
}

/**
 * A status that does not refuse a topic: 429, rate limiting, and every status of 500 or above, a
 * server or gateway failure. A call or a topic answered with one is sent again: a poll on the next
 * rung, a subscribe on the next reconnect, with the topic polled meanwhile.
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

  /** Pushes that arrived before the first answer gave the topic a rev. */
  pending: Arrival[] = [];

  /** A gap poll of the topic is in flight. */
  gapInFlight = false;

  /** The largest rev a gap push named while a gap poll was in flight. */
  gapRev: number | null = null;

  /**
   * A gap poll or a check poll of the topic failed without refusing it, or its answer did not answer
   * the topic: the held rev may be below the BE's. The ladder runs, also over an open peer, until a poll answers
   * the topic.
   */
  behind = false;

  constructor(readonly topic: string) {}
}

class EventClientImpl implements EventClient {
  readonly basePath: string;

  private readonly peer?: EventPeerLike;

  private readonly http?: EventAxiosLike;

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
    this.peer = options.peer;
    this.http = options.axios;
    this.pollSettings = options.pollSettings ?? DEFAULT_POLL_SETTINGS;
    this.draw = options.draw ?? Math.random;
    this.ladder = new PollLadder(this.pollSettings, this.draw);
    this.checkIntervalMs = options.checkIntervalMs ?? DEFAULT_CHECK_INTERVAL_MS;
    this.peer?.onClose?.(() => this.onPeerClose());
    this.peer?.onReconnect?.(() => this.onPeerReconnect());
  }

  // -- subscriptions --------------------------------------------------------

  subscribe<P>(topic: string, event: EventDefinition<P>, handlers: EventHandlers<NoInfer<P>>): EventSubscription {
    if (this.closed) throw new Error('The event client is closed');
    let state = this.topics.get(topic);
    if (!state) {
      state = new TopicState(topic);
      this.topics.set(topic, state);
      if (this.peerOpen()) this.sendSubscribe(state);
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

  /** Sets the first rev of a topic, replays the pushes held for it and resolves its subscriptions. */
  private follow(state: TopicState, rev: number): void {
    state.rev = rev;
    const pending = state.pending;
    state.pending = [];
    for (const sub of state.subs) sub.resolve();
    for (const arrival of pending) {
      // A handler of a replayed push may have ended the following of the topic.
      if (this.topics.get(state.topic) !== state) return;
      this.receive(state, arrival);
    }
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
      });
      for (const sub of [...state.subs]) {
        if (sub.active && sub.event === arrival.event) runHandler(() => sub.handlers.onEvent(arrival.payload, meta));
      }
      return 'applied';
    }
    return arrival.rev <= held ? 'ignored' : 'gap';
  }

  /** A pushed event: held until the topic has a rev, then applied, ignored or answered with a gap poll. */
  private receive(state: TopicState, arrival: Arrival): void {
    if (state.rev === null) {
      state.pending.push(arrival);
      return;
    }
    if (this.apply(state, arrival) === 'gap') this.gapPoll(state, arrival.rev);
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
   * One topic of a poll answer. `cursor` is the rev the poll carried; the held rev may have moved
   * since, by pushes applied while the poll was in flight.
   *
   * - `error` with a transient status (`isTransientStatus`) leaves the topic as it is; any other
   *   `error` refuses the topic.
   * - The first answer of a topic sets its rev.
   * - `events: null` is a resync, unless pushes have moved the held rev since the poll was sent and
   *   the answer's rev is at most the held one: those pushes applied the events the log no longer has.
   * - An answer's rev below the cursor is a resync.
   * - Otherwise each event is applied or ignored by the rule of a push, except that an event whose
   *   `prev` is below the held rev and whose rev is above it is applied: the BE chains the events of
   *   an answer, so a `prev` below the held rev names an event the held rev already covers. A
   *   malformed event, one without a usable rev, prev, event name or version, and an event that is
   *   neither applied nor ignored, a gap inside a poll answer, are a resync. After the events the
   *   held rev becomes the answer's rev when that is larger: the BE leaves out of an answer the
   *   events its delivery filters refuse, and the answer's rev covers them.
   *
   * Returns `'moved'` when the held rev moved, `'answered'` when the answer answered the topic
   * without moving it, and `'unanswered'` for a transient error and for an answer without a rev.
   */
  private applyTopicAnswer(
    state: TopicState,
    cursor: number | null,
    answer: WireTopicAnswer,
  ): 'moved' | 'answered' | 'unanswered' {
    if (typeof answer.error === 'string' && answer.error) {
      const error = topicError(state.topic, answer.error);
      if (isTransientStatus(error.response.status)) return 'unanswered';
      this.refuse(state, error);
      return 'answered';
    }
    const rev = revOf(answer.rev);
    if (rev === null) return 'unanswered';
    state.behind = false;
    if (state.rev === null) {
      this.follow(state, rev);
      return 'answered';
    }
    const held = state.rev;
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
      const arrival = toArrival(event);
      if (arrival === null || this.apply(state, chained(arrival, state.rev)) === 'gap') {
        this.resync(state, rev);
        return 'moved';
      }
    }
    // The answer covers every event up to its rev; the ones it leaves out were not for this client.
    if (rev > state.rev) state.rev = rev;
    return state.rev !== held ? 'moved' : 'answered';
  }

  /**
   * Receives a pushed event; see `handleEventStream`. A push without a string `:topic` and `:event`,
   * or without a usable `:rev`, `:prev` or `:version`, is ignored.
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
    this.receive(state, { rev, prev, event, version, payload });
  }

  // -- calls -------------------------------------------------------------------

  private peerOpen(): boolean {
    if (!this.peer) return false;
    return this.peer.isOpen ?? this.trackedOpen;
  }

  /**
   * One call to `{basePath}/{name}`: one muxws stream while the peer is open, one axios POST
   * otherwise. `subscribe` and `unsubscribe` exist on the socket only and are refused without one.
   */
  private async call<R>(name: 'subscribe' | 'unsubscribe' | 'poll', body: unknown): Promise<R> {
    const path = `${this.basePath}/${name}`;
    if (this.peer && this.peerOpen()) {
      const stream = this.peer.open(body, { headers: { ':method': 'POST', ':path': path }, end: true });
      const result = stream.result();
      await Promise.race([stream.replyHeadersArrived, result]);
      const status = readStatus(stream.replyHeaders);
      const data = await result;
      if (status >= 400) throw new ViewSetRequestError(status, data, readHeaders(stream.replyHeaders));
      return data as R;
    }
    if (name !== 'poll' || !this.http) throw new Error(`The event client has no transport for '${name}'`);
    const response = await this.http.post<R>(path, body);
    return response.data;
  }

  /**
   * Runs `op`, a `subscribe` or `unsubscribe` of `topic` on the peer's socket, once the socket call
   * of `topic` before it has settled, whichever client of the peer sent that one. The BE serves the
   * streams of one socket concurrently: an `unsubscribe` answered after a later `subscribe` of the
   * same topic ends that subscription. `op` opens its stream synchronously when no call of the topic
   * is in flight.
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
      try {
        answer = await this.call<WireSubscribeAnswer>('subscribe', { topic });
      } catch (error) {
        if (this.closed || generation !== this.socketGeneration || this.topics.get(topic) !== state) return;
        state.socket = 'none';
        if (isRefusal(error)) this.refuse(state, error);
        else this.ensureLoop();
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
        if (state.rev === null) this.follow(state, rev);
        else if (rev > state.rev) this.gapPoll(state, rev);
        else state.behind = false;
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
    void this.call<WirePollAnswer>('poll', { cursors: { [state.topic]: cursor } }).then(
      (answer) => {
        state.gapInFlight = false;
        if (this.closed || this.topics.get(state.topic) !== state) return;
        const entry = answer?.topics?.[state.topic];
        if (entry && this.applyTopicAnswer(state, cursor, entry) !== 'unanswered') this.gapAgain(state);
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
   * subscribed on its socket or is `behind`, or the peer is not open and there is axios to poll over.
   */
  private shouldPoll(): boolean {
    if (this.closed || this.topics.size === 0) return false;
    if (this.peerOpen()) {
      return [...this.topics.values()].some((state) => state.socket !== 'subscribed' || state.behind);
    }
    return this.http !== undefined;
  }

  /**
   * A topic without a rev that no poll has carried is polled at once when there is no open peer to
   * subscribe it on.
   */
  private pollNow(): boolean {
    return !this.peerOpen() && [...this.topics.values()].some((state) => state.rev === null && state.unpolled);
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

  /** One rung: one poll of every followed topic, then the next rung's delay from the ladder. */
  private async rung(): Promise<void> {
    if (!this.shouldPoll()) {
      this.loop = 'idle';
      return;
    }
    this.loop = 'polling';
    const sent = [...this.topics.values()].map((state) => ({ state, cursor: state.rev }));
    const cursors: Record<string, number | null> = {};
    for (const { state, cursor } of sent) {
      cursors[state.topic] = cursor;
      state.unpolled = false;
    }

    let revChanged = false;
    let pollAfterMs: number | null = null;
    try {
      const answer = await this.call<WirePollAnswer>('poll', { cursors });
      if (this.closed) return;
      pollAfterMs = typeof answer?.poll_after_ms === 'number' ? answer.poll_after_ms : null;
      for (const { state, cursor } of sent) {
        if (this.topics.get(state.topic) !== state) continue;
        const entry = answer?.topics?.[state.topic];
        if (entry && this.applyTopicAnswer(state, cursor, entry) === 'moved') revChanged = true;
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
   * One poll over the open peer of every topic that is subscribed on its socket, has a rev, is not
   * `behind` and has no gap poll in flight, while the ladder is idle; a tick without such a topic, or
   * with the ladder running or the peer not open, sends nothing. The answer is applied as a rung's.
   * A topic the answer does not answer, and every topic of a poll that fails without refusing them,
   * fall behind to the ladder. The next check is scheduled after the answer, while the client
   * follows a topic.
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
    const cursors: Record<string, number | null> = {};
    for (const { state, cursor } of sent) cursors[state.topic] = cursor;
    try {
      const answer = await this.call<WirePollAnswer>('poll', { cursors });
      if (this.closed) return;
      for (const { state, cursor } of sent) {
        if (this.topics.get(state.topic) !== state) continue;
        const entry = answer?.topics?.[state.topic];
        if (!entry || this.applyTopicAnswer(state, cursor, entry) === 'unanswered') this.checkBehind(state, generation);
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

/** A poll answer entry as an `Arrival`; `null` for an entry without a usable rev, prev, event name or version. */
function toArrival(event: WireEvent): Arrival | null {
  const rev = revOf(event?.rev);
  const prev = revOf(event?.prev);
  const version = versionOf(event?.version);
  if (rev === null || prev === null || version === null || typeof event.event !== 'string') return null;
  return { rev, prev, event: event.event, version, payload: event.payload };
}

/** A topic error of a poll answer, as the `ViewSetRequestError` it is reported with. */
function topicError(topic: string, code: string): ViewSetRequestError {
  return new ViewSetRequestError(TOPIC_ERROR_STATUS[code] ?? 403, {
    detail: `The poll of topic '${topic}' answered ${code}`,
    detail_code: code,
    detail_params: { topic },
  });
}

// ---------------------------------------------------------------------------
// Registry and stream dispatch
// ---------------------------------------------------------------------------

const registry = new WeakMap<object, EventClientImpl>();

/** The socket calls of every client of a peer in flight, by topic; see `EventClientImpl.onSocket`. */
const socketCalls = new WeakMap<object, Map<string, Promise<void>>>();

/**
 * Builds an event client. Throws without `peer` and without `axios`, and for a `peer` that already
 * has a client; the client is registered for its `peer` until `close()`.
 */
export function createEventClient(options: EventClientOptions): EventClient {
  if (!options.peer && !options.axios) throw new Error('createEventClient needs a peer, axios, or both');
  if (options.peer && registry.has(options.peer)) throw new Error('This peer already has an event client');
  const client = new EventClientImpl(options);
  if (options.peer) registry.set(options.peer, client);
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
 * A subscription made once `client` resolves: what `watch()` returns on a muxws ViewSet whose peer
 * source has not resolved yet. `unsubscribe()` before then subscribes nothing and reports nothing,
 * and `ready` resolves once `client` does.
 *
 * When `client` rejects, or `subscribe` throws, `ready` rejects with that error, and the error is
 * passed to `onError`, or rethrown by `rethrowLater` when the subscription has no `onError`.
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
