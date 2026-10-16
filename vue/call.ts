/**
 * What every call through a proxy shares, whichever transport sends it: the `Idempotency-Key`, the
 * promise that carries it (`ViewSetCall`), and the repeating of an attempt that ended without an
 * answer.
 *
 * The key follows draft-ietf-httpapi-idempotency-key-header-07: a fresh UUID per call, sent as a
 * Structured Field string, and the same key on every repeat of that call. The BE's `Idempotency`
 * middleware answers a repeat with the stored outcome of the first attempt, or attaches it to the
 * attempt that is still running. Where it has neither (no `Idempotency` on the BE, a `GET` outcome
 * that was not stored, an outcome that set a cookie, an expired record), the repeat runs the action.
 */

/** The header that carries the key. */
export const IDEMPOTENCY_KEY_HEADER = 'Idempotency-Key';

/**
 * The promise a call returns, with the call's `Idempotency-Key`.
 *
 * `key` is readable synchronously, before the call settles, and is the value an application passes
 * as `RequestOptions.key` to repeat the call itself.
 */
export type ViewSetCall<R> = Promise<R> & { readonly key: string };

/**
 * What a ViewSet's actions return in their declared type: a `ViewSetCall` from the built-in actions,
 * and whatever an override returns.
 *
 * `key` is optional in the type because an override is a method returning `Promise<R>`. At run time
 * the proxy sets `key` on the promise of every method that issues a request before its first
 * `await`, overrides included; see `bindCallKeys`.
 */
export type ViewSetActionCall<R> = Promise<R> & { readonly key?: string };

/**
 * `M` with every method that returns `Promise<R>` returning `ViewSetCall<R>` instead; the type
 * `route_rest` and `route_muxws` hand back. Other members are unchanged.
 */
export type ViewSetCalls<M> = {
  [P in keyof M]: M[P] extends (...args: infer A) => Promise<infer R> ? (...args: A) => ViewSetCall<R> : M[P];
};

/** When and how long a proxy repeats a call that ended without an answer. */
export interface RepeatOptions {
  /**
   * How long after the first attempt a repeat may still be sent, in milliseconds. Past it, the call
   * rejects with the error of its last attempt. `0` turns repeating off. Default 600000.
   */
  withinMs?: number;
  /**
   * The delay before the first repeat after a lost connection or a gateway status, in milliseconds.
   * Doubled after each such repeat, up to 30000. A repeat after an attempt's own timeout is sent
   * without a delay. Default 1000.
   */
  delayMs?: number;
}

const DEFAULT_REPEAT_WITHIN_MS = 600_000;
const DEFAULT_REPEAT_DELAY_MS = 1000;
const MAX_REPEAT_DELAY_MS = 30_000;

/**
 * How an attempt that ended in `error` is followed up:
 * - `'timeout'`: the attempt's own timeout expired; repeat at once.
 * - `'lost'`: no answer reached the client (a lost connection, a refused stream, a gateway status);
 *   repeat after the current delay.
 * - `null`: an answer; the call settles with it.
 */
export type AttemptFailure = 'timeout' | 'lost' | null;

/** One attempt of a call: its outcome, and how to stop it once it is superseded. */
export interface CallAttempt<R> {
  readonly result: Promise<R>;
  abandon(): void;
}

// ---------------------------------------------------------------------------
// The key
// ---------------------------------------------------------------------------

/**
 * A version 4 UUID. `crypto.randomUUID()` exists only in a secure context (HTTPS, localhost); the
 * same value is built from `crypto.getRandomValues()` everywhere else.
 */
export function newCallKey(): string {
  const { crypto } = globalThis;
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/**
 * `key` as a Structured Field string (RFC 8941): quoted, with `"` and `\` escaped.
 *
 * Throws a TypeError for an empty key or one with a character outside printable ASCII, which an
 * sf-string cannot carry and the BE rejects with `400 idempotency_key_invalid`.
 */
export function idempotencyKeyHeader(key: string): string {
  if (!/^[\x20-\x7e]+$/.test(key)) {
    throw new TypeError(`An idempotency key must be a non-empty string of printable ASCII, got ${JSON.stringify(key)}`);
  }
  return `"${key.replace(/[\\"]/g, '\\$&')}"`;
}

/**
 * `headers` with the `Idempotency-Key` of `key` and no other spelling of that header name: header
 * names are case-insensitive, and a second spelling would send the header twice.
 */
export function withKeyHeader<V>(headers: Record<string, V>, key: string): Record<string, V | string> {
  const out: Record<string, V | string> = {};
  for (const [name, value] of Object.entries(headers)) {
    if (name.toLowerCase() !== IDEMPOTENCY_KEY_HEADER.toLowerCase()) out[name] = value;
  }
  out[IDEMPOTENCY_KEY_HEADER] = idempotencyKeyHeader(key);
  return out;
}

// ---------------------------------------------------------------------------
// ViewSetCall
// ---------------------------------------------------------------------------

/** The key slot of the innermost method call `bindCallKeys` is running synchronously, if any. */
let capturing: { key?: string } | null = null;

/**
 * Sets `key` on `promise` and reports it to the method running synchronously around this call, so
 * that its own promise gets the key of the first request it issues. Transports call this from
 * `request()`, synchronously.
 *
 * Reports the call to the `onCallActivity` listeners: `start` at once, `settle` when `promise`
 * settles. `background` is passed on in `start`.
 */
export function issueCall<R>(promise: Promise<R>, key: string, background = false): ViewSetCall<R> {
  if (capturing && capturing.key === undefined) capturing.key = key;
  emit({ type: 'start', key, background });
  promise.then(
    () => emit({ type: 'settle', key, ok: true }),
    () => emit({ type: 'settle', key, ok: false }),
  );
  return withKey(promise, key);
}

function withKey<R>(promise: Promise<R>, key: string): ViewSetCall<R> {
  Object.defineProperty(promise, 'key', { value: key, enumerable: true });
  return promise as ViewSetCall<R>;
}

function isPromise(value: unknown): value is Promise<unknown> {
  return value instanceof Promise;
}

/**
 * Members `bindCallKeys` leaves alone: the constructor, and `request()`, whose promise already
 * carries its key.
 */
const UNBOUND_MEMBERS = new Set(['constructor', 'request']);

/**
 * Replaces every method of `instance`'s class chain, up to and excluding `stopAt`, with an own
 * property that calls it and sets `key` on the promise it returns: the key of the first request the
 * method issues while it runs synchronously, that is, before its first `await`. A method that issues
 * no request in that part returns its promise unchanged.
 *
 * Nested calls report outward: `listPage()` calling the private `requestList()` calling `request()`
 * gives all three promises the same key.
 */
export function bindCallKeys(instance: object, stopAt: object): void {
  const seen = new Set<string>();
  for (let proto = Object.getPrototypeOf(instance); proto && proto !== stopAt; proto = Object.getPrototypeOf(proto)) {
    for (const name of Object.getOwnPropertyNames(proto)) {
      if (seen.has(name) || UNBOUND_MEMBERS.has(name)) continue;
      seen.add(name);
      const descriptor = Object.getOwnPropertyDescriptor(proto, name);
      if (typeof descriptor?.value !== 'function') continue;
      Object.defineProperty(instance, name, {
        value: keyedMethod(descriptor.value as (...args: unknown[]) => unknown),
        writable: true,
        configurable: true,
      });
    }
  }
}

function keyedMethod(method: (...args: unknown[]) => unknown): (...args: unknown[]) => unknown {
  return function (this: unknown, ...args: unknown[]): unknown {
    const outer = capturing;
    const own: { key?: string } = {};
    capturing = own;
    let result: unknown;
    try {
      result = method.apply(this, args);
    } finally {
      capturing = outer;
    }
    if (own.key !== undefined) {
      if (outer && outer.key === undefined) outer.key = own.key;
      if (isPromise(result) && !('key' in result)) withKey(result, own.key);
    }
    return result;
  };
}

// ---------------------------------------------------------------------------
// Call activity
// ---------------------------------------------------------------------------

/**
 * What happens to a call issued through a proxy, as `onCallActivity` reports it.
 *
 * - `start`: the call was issued. `background` is `RequestOptions.background`.
 * - `retry`: an attempt ended without an answer and is repeated. `failure` is how it ended,
 *   `attempt` the number of the attempt that ended (1 for the first), `delayMs` the wait before the
 *   repeat is sent.
 * - `settle`: the call settled; `ok` is false when it rejected.
 */
export type CallEvent =
  | { readonly type: 'start'; readonly key: string; readonly background: boolean }
  | {
      readonly type: 'retry';
      readonly key: string;
      readonly failure: 'timeout' | 'lost';
      readonly attempt: number;
      readonly delayMs: number;
    }
  | { readonly type: 'settle'; readonly key: string; readonly ok: boolean };

/** A call issued through a proxy that has not settled, as `pendingCalls()` lists it. */
export interface PendingCall {
  readonly key: string;
  readonly background: boolean;
  /** `Date.now()` when the call was issued. */
  readonly startedAt: number;
  /** The attempts that ended without an answer and were repeated. */
  readonly unanswered: number;
}

const listeners = new Set<(event: CallEvent) => void>();

/**
 * The unsettled calls, in the order they were issued. Two calls may share a key (an application that
 * repeats a call itself while the first is pending); an event of that key applies to the older one.
 */
const pending: PendingCall[] = [];

/** Every call issued through a proxy that has not settled, oldest first. */
export function pendingCalls(): readonly PendingCall[] {
  return [...pending];
}

/** The number of unsettled calls issued through a proxy; background calls only with `includeBackground`. */
export function pendingCallCount(includeBackground = false): number {
  return includeBackground ? pending.length : pending.filter((call) => !call.background).length;
}

function track(event: CallEvent): void {
  if (event.type === 'start') {
    pending.push({ key: event.key, background: event.background, startedAt: Date.now(), unanswered: 0 });
    return;
  }
  const index = pending.findIndex((call) => call.key === event.key);
  if (index < 0) return;
  if (event.type === 'retry') pending[index] = { ...pending[index], unanswered: pending[index].unanswered + 1 };
  else pending.splice(index, 1);
}

/**
 * Calls `listener` for every `CallEvent` of every call issued through a proxy, REST and muxws alike.
 * Returns the function that removes it. A listener that throws is logged with `console.error` and
 * does not affect the call or the other listeners.
 */
export function onCallActivity(listener: (event: CallEvent) => void): () => void {
  const entry = (event: CallEvent): void => listener(event);
  listeners.add(entry);
  return () => {
    listeners.delete(entry);
  };
}

function emit(event: CallEvent): void {
  track(event);
  for (const listener of [...listeners]) {
    try {
      listener(event);
    } catch (error) {
      console.error('fastapi-viewsets: an onCallActivity listener threw', error);
    }
  }
}

// ---------------------------------------------------------------------------
// Repeats
// ---------------------------------------------------------------------------

/** The wake-ups of the repeat delays now running. */
const sleepers = new Set<() => void>();

/** Resolves after `ms`, or at once on `retryNow()`, with true when `retryNow()` ended it. */
function repeatDelay(ms: number): Promise<boolean> {
  return new Promise((resolve) => {
    const wake = (): void => {
      clearTimeout(timer);
      sleepers.delete(wake);
      resolve(true);
    };
    const timer = setTimeout(() => {
      sleepers.delete(wake);
      resolve(false);
    }, ms);
    sleepers.add(wake);
  });
}

/**
 * Ends the delay of every call waiting to send a repeat, so each sends it now; the delay of such a
 * call's next repeat starts again from `repeat.delayMs`. A muxws call waiting for its peer to
 * reconnect keeps waiting.
 */
export function retryNow(): void {
  for (const wake of [...sleepers]) wake();
}

/**
 * Runs a call: starts the first attempt synchronously, and starts a new one with `start()` while the
 * previous one ended in a failure `classify` reports and `repeat.withinMs` has not elapsed since the
 * first attempt. The promise settles with the outcome of the last attempt only; every attempt that is
 * repeated is abandoned first.
 *
 * `beforeRepeat` runs after the delay and before the repeat is started, and is given the deadline
 * (`Date.now()` scale) past which no repeat is sent; muxws waits there for the peer to reconnect.
 *
 * A `start()` that throws counts as an attempt that failed with that error. Every repeat is reported
 * to the `onCallActivity` listeners as a `retry` of `key`.
 */
export async function runCall<R>(
  start: () => CallAttempt<R>,
  classify: (error: unknown) => AttemptFailure,
  repeat: RepeatOptions | undefined,
  beforeRepeat?: (deadline: number) => Promise<void>,
  key = '',
): Promise<R> {
  const withinMs = repeat?.withinMs ?? DEFAULT_REPEAT_WITHIN_MS;
  const firstDelayMs = repeat?.delayMs ?? DEFAULT_REPEAT_DELAY_MS;
  let delayMs = firstDelayMs;
  const deadline = Date.now() + withinMs;

  for (let attemptNumber = 1; ; attemptNumber += 1) {
    const attempt = startAttempt(start);
    try {
      return await attempt.result;
    } catch (error) {
      const failure = withinMs > 0 ? classify(error) : null;
      if (failure === null) throw error;
      attempt.abandon();

      const wait = failure === 'timeout' ? 0 : delayMs;
      if (failure === 'lost') delayMs = Math.min(delayMs * 2, MAX_REPEAT_DELAY_MS);
      if (Date.now() + wait >= deadline) throw error;
      emit({ type: 'retry', key, failure, attempt: attemptNumber, delayMs: wait });
      if (wait > 0 && (await repeatDelay(wait))) delayMs = firstDelayMs;
      if (beforeRepeat) await beforeRepeat(deadline);
      if (Date.now() >= deadline) throw error;
    }
  }
}

function startAttempt<R>(start: () => CallAttempt<R>): CallAttempt<R> {
  try {
    return start();
  } catch (error) {
    return { result: Promise.reject(error), abandon: () => {} };
  }
}
