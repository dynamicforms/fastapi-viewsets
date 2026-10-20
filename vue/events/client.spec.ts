/**
 * Unit tests for events.ts: the event client against a fake peer and a fake axios, with fake timers.
 *
 * Every call the client sends is recorded with its transport, path and body, and stays pending until
 * the test answers it, so each rule is driven one arrival at a time.
 */

import axios from 'axios';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ViewSetRequestError } from '../errors';

import {
  attachEvents,
  createEventClient,
  defineEvent,
  type EventAxiosLike,
  type EventClient,
  type EventClientOptions,
  type EventMeta,
  type EventPeerLike,
  type EventStreamHandler,
  eventsOf,
  handleEventStream,
} from './client';

interface SentCall {
  via: 'peer' | 'axios';
  path: string;
  body: any;
  headers?: Record<string, unknown>;
  end?: boolean;
  answer(body: unknown, status?: number): void;
  fail(error: unknown): void;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function makeTransport() {
  const calls: SentCall[] = [];
  const closeHandlers: ((reason: { willRetry: boolean }) => void)[] = [];
  const reconnectHandlers: (() => void)[] = [];
  let streamHandler: EventStreamHandler | null = null;

  const peer: EventPeerLike & { isOpen: boolean } = {
    isOpen: true,
    open(payload, options) {
      const headers = (options?.headers ?? {}) as Record<string, unknown>;
      const body = deferred<unknown>();
      const replyHeaders: Record<string, unknown> = {};
      const arrived = deferred<void>();
      calls.push({
        via: 'peer',
        path: headers[':path'] as string,
        body: payload,
        headers,
        end: options?.end,
        answer(data, status = 200) {
          replyHeaders[':status'] = status;
          arrived.resolve();
          body.resolve(data);
        },
        fail(error) {
          arrived.resolve();
          body.reject(error);
        },
      });
      return { result: () => body.promise, replyHeadersArrived: arrived.promise, replyHeaders };
    },
    onClose(handler) {
      closeHandlers.push(handler);
    },
    onReconnect(handler) {
      reconnectHandlers.push(handler);
    },
    onStream(handler) {
      streamHandler = handler;
    },
  };

  const axios = {
    post: vi.fn((path: string, body: unknown, config?: { headers?: Record<string, string> }) => {
      const response = deferred<{ data: any }>();
      calls.push({
        via: 'axios',
        path,
        body,
        headers: config?.headers,
        answer(data, status = 200) {
          if (status >= 400) response.reject(Object.assign(new Error('axios'), { response: { status, data } }));
          else response.resolve({ data });
        },
        fail: response.reject,
      });
      return response.promise;
    }),
  };

  return {
    calls,
    peer,
    axios,
    get streamHandler() {
      return streamHandler;
    },
    /** The calls not yet looked at by `take()`. */
    taken: 0,
    take(): SentCall[] {
      const out = calls.slice(this.taken);
      this.taken = calls.length;
      return out;
    },
    close() {
      peer.isOpen = false;
      for (const handler of closeHandlers) handler({ willRetry: true });
    },
    reconnect() {
      peer.isOpen = true;
      for (const handler of reconnectHandlers) handler();
    },
    closeForGood() {
      peer.isOpen = false;
      Object.assign(peer, { isClosed: true });
      for (const handler of closeHandlers) handler({ willRetry: false });
    },
    push(topic: string, rev: number, prev: number, event: string, payload: unknown, version: unknown = 1) {
      return handleEventStream(peer, payload, {
        headers: { ':event': event, ':version': version, ':topic': topic, ':rev': rev, ':prev': prev },
      });
    },
    /** A push of a scoped topic carrying `:scope`. */
    pushScoped(topic: string, scope: string, rev: number, prev: number, payload: unknown = { n: rev }) {
      const headers = {
        ':event': 'changed',
        ':version': 1,
        ':topic': topic,
        ':rev': rev,
        ':prev': prev,
        ':scope': scope,
      };
      return handleEventStream(peer, payload, { headers });
    },
  };
}

type Transport = ReturnType<typeof makeTransport>;

const Changed = defineEvent<{ n: number }>('changed');
const Other = defineEvent<{ x: string }>('other');

function handlers() {
  return { onEvent: vi.fn(), onResync: vi.fn(), onError: vi.fn() };
}

/** The payloads `h.onEvent` was called with, in order. */
function payloads(h: { onEvent: ReturnType<typeof vi.fn> }): unknown[] {
  return h.onEvent.mock.calls.map(([payload]) => payload);
}

const flush = () => vi.advanceTimersByTimeAsync(0);

/** `draw: () => 0.5` makes the jitter multiplier exactly 1, so every delay is the ladder's own. */
function client(transport: Transport, options: Partial<EventClientOptions> = {}): EventClient {
  return createEventClient({
    basePath: '/events',
    peer: transport.peer,
    draw: () => 0.5,
    checkIntervalMs: 0,
    ...options,
  });
}

let clients: EventClient[] = [];
function track(c: EventClient): EventClient {
  clients.push(c);
  return c;
}

beforeEach(() => {
  vi.useFakeTimers();
  clients = [];
});

afterEach(() => {
  for (const c of clients) c.close();
  vi.useRealTimers();
});

describe('types', () => {
  it('rejects a handler whose payload type is not the definition one, a wider one included', () => {
    const t = makeTransport();
    const c = track(client(t));
    const ImportFinished = defineEvent<{ file: string; rows: number }>('import_finished');
    c.subscribe('imports:7', ImportFinished, {
      // @ts-expect-error -- the parameter type has a field the payload type lacks
      onEvent(payload: { file: string; rows: number; extra: boolean }) {
        void payload;
      },
      onResync() {},
    });
    c.subscribe('imports:7', ImportFinished, {
      // @ts-expect-error -- the parameter type has a field the payload type lacks
      onEvent: (payload: { file: string; rows: number; extra: boolean }) => void payload,
      onResync() {},
    });
    c.subscribe('imports:7', defineEvent('untyped'), {
      // @ts-expect-error -- an untyped definition has an `unknown` payload
      onEvent: (payload: { file: string }) => void payload,
      onResync() {},
    });
    c.subscribe('imports:7', ImportFinished, {
      onEvent(payload) {
        const rows: number = payload.rows;
        void rows;
      },
      onResync() {},
    });
    c.subscribe('imports:7', ImportFinished, {
      onEvent(payload, meta) {
        const version: number = meta.version;
        const topic: string = meta.topic;
        void [payload, version, topic, meta.rev, meta.prev];
      },
      onResync() {},
    });
    c.subscribe('imports:7', ImportFinished, {
      // @ts-expect-error -- the meta parameter type is not EventMeta
      onEvent(payload, meta: { version: string }) {
        void [payload, meta];
      },
      onResync() {},
    });
  });
});

describe('createEventClient and eventsOf', () => {
  it('throws without a peer and without axios', () => {
    expect(() => createEventClient({ basePath: '/events' })).toThrow(/peer, axios, or both/);
  });

  it('registers the client for its peer, and a second client for the same peer throws', () => {
    const t = makeTransport();
    const c = track(client(t));
    expect(eventsOf(t.peer)).toBe(c);
    expect(() => client(t)).toThrow(/already has an event client/);
  });

  it('close() unregisters the client, so the peer may be given a new one', () => {
    const t = makeTransport();
    client(t).close();
    expect(eventsOf(t.peer)).toBeUndefined();
    expect(() => track(client(t))).not.toThrow();
  });

  it('takes an AxiosInstance as axios', () => {
    const instance: EventAxiosLike = axios.create();
    expect(typeof instance.post).toBe('function');
  });

  it('an axios-only client is registered for no peer', () => {
    const t = makeTransport();
    track(createEventClient({ basePath: '/events', axios: t.axios }));
    expect(eventsOf(t.peer)).toBeUndefined();
  });

  it('subscribe() on a closed client throws', () => {
    const t = makeTransport();
    const c = client(t);
    c.close();
    expect(() => c.subscribe('a', Changed, handlers())).toThrow(/closed/);
  });
});

describe('subscribing over an open peer', () => {
  it('sends one POST stream to {basePath}/subscribe, with end and without Idempotency-Key', async () => {
    const t = makeTransport();
    const c = track(client(t));
    c.subscribe('/api/music', Changed, handlers());
    const [call] = t.take();
    expect(call.via).toBe('peer');
    expect(call.headers).toEqual({ ':method': 'POST', ':path': '/events/subscribe' });
    expect(call.end).toBe(true);
    expect(call.body).toEqual({ topic: '/api/music' });
  });

  it('ready resolves on the answer; a second subscription of the topic sends nothing, ready at once', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const first = c.subscribe('a', Changed, handlers());
    let ready = false;
    void first.ready.then(() => (ready = true));
    await flush();
    expect(ready).toBe(false);
    t.take()[0].answer({ rev: 5 });
    await flush();
    expect(ready).toBe(true);

    const second = c.subscribe('a', Other, handlers());
    await expect(second.ready).resolves.toBeUndefined();
    expect(t.take()).toEqual([]);
  });

  it('a refused subscribe rejects ready with ViewSetRequestError, calls onError, removes it', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const h = handlers();
    const sub = c.subscribe('a', Changed, h);
    t.take()[0].answer({ detail: 'refused', detail_code: 'fastapi-viewsets.event_topic_refused' }, 403);
    await expect(sub.ready).rejects.toBeInstanceOf(ViewSetRequestError);
    await expect(sub.ready).rejects.toMatchObject({ response: { status: 403 } });
    expect(h.onError).toHaveBeenCalledWith(expect.any(ViewSetRequestError));

    t.push('a', 6, 5, 'changed', { n: 1 });
    expect(h.onEvent).not.toHaveBeenCalled();
    // A new subscription to the topic subscribes again.
    c.subscribe('a', Changed, handlers());
    expect(t.take()).toHaveLength(1);
  });

  it('a subscribe that fails without a status leaves the topic polled over the peer', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const sub = c.subscribe('a', Changed, handlers());
    t.take()[0].fail(Object.assign(new Error('lost'), { name: 'ConnectionLost' }));
    await flush();
    await vi.advanceTimersByTimeAsync(400);
    const [poll] = t.take();
    expect(poll).toMatchObject({ via: 'peer', path: '/events/poll', body: { cursors: { a: null }, scopes: {} } });
    poll.answer({ topics: { a: { rev: 3, events: [] } }, poll_after_ms: 0 });
    await expect(sub.ready).resolves.toBeUndefined();
  });
});

/** A client following topic `a` at rev 10 over an open peer. */
async function followed(t: Transport, topic = 'a') {
  const c = track(client(t, { axios: t.axios }));
  const h = handlers();
  const sub = c.subscribe(topic, Changed, h);
  t.take()[0].answer({ rev: 10 });
  await sub.ready;
  return { c, h, sub };
}

describe('push, gap, poll and resync', () => {
  it('applies a push whose prev is the held rev; the held rev becomes the push rev', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    expect(t.push('a', 11, 10, 'changed', { n: 1 })).toBe(true);
    expect(t.push('a', 12, 11, 'changed', { n: 2 })).toBe(true);
    expect(payloads(h)).toEqual([{ n: 1 }, { n: 2 }]);
    expect(t.take()).toEqual([]);
  });

  it('ignores a push whose rev is at most the held one', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 10, 9, 'changed', { n: 1 });
    t.push('a', 7, 3, 'changed', { n: 2 });
    expect(h.onEvent).not.toHaveBeenCalled();
    expect(t.take()).toEqual([]);
  });

  it('answers a push with a larger rev and another prev with one poll of that topic from the held rev', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 13, 12, 'changed', { n: 13 });
    const calls = t.take();
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({ via: 'peer', path: '/events/poll', body: { cursors: { a: 10 }, scopes: {} } });

    calls[0].answer({
      topics: {
        a: {
          rev: 13,
          events: [
            { rev: 11, prev: 10, event: 'changed', version: 1, payload: { n: 11 } },
            { rev: 12, prev: 11, event: 'changed', version: 1, payload: { n: 12 } },
            { rev: 13, prev: 12, event: 'changed', version: 1, payload: { n: 13 } },
          ],
        },
      },
      poll_after_ms: 0,
    });
    await flush();
    expect(payloads(h)).toEqual([{ n: 11 }, { n: 12 }, { n: 13 }]);
    expect(h.onResync).not.toHaveBeenCalled();
    // The held rev is 13: the next push chains on it.
    t.push('a', 14, 13, 'changed', { n: 14 });
    expect(h.onEvent).toHaveBeenLastCalledWith({ n: 14 }, expect.anything());
  });

  it('sends one gap poll while one is in flight, and another after it when the held rev is still behind', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 13, 12, 'changed', {});
    t.push('a', 15, 14, 'changed', {});
    const [first] = t.take();
    expect(t.take()).toEqual([]);
    first.answer({
      topics: {
        a: {
          rev: 13,
          events: [
            { rev: 11, prev: 10, event: 'changed', version: 1, payload: {} },
            { rev: 12, prev: 11, event: 'changed', version: 1, payload: {} },
            { rev: 13, prev: 12, event: 'changed', version: 1, payload: {} },
          ],
        },
      },
    });
    await flush();
    const [second] = t.take();
    expect(second.body).toEqual({ cursors: { a: 13 }, scopes: {} });
    second.answer({
      topics: {
        a: {
          rev: 15,
          events: [
            { rev: 14, prev: 13, event: 'changed', version: 1, payload: {} },
            { rev: 15, prev: 14, event: 'changed', version: 1, payload: {} },
          ],
        },
      },
    });
    await flush();
    expect(h.onEvent).toHaveBeenCalledTimes(5);
    expect(t.take()).toEqual([]);
  });

  it('answers events: null with onResync on every subscription; the held rev becomes the answer rev', async () => {
    const t = makeTransport();
    const { c, h } = await followed(t);
    const other = handlers();
    c.subscribe('a', Other, other);
    t.push('a', 20, 19, 'changed', {});
    t.take()[0].answer({ topics: { a: { rev: 20, events: null } } });
    await flush();
    expect(h.onResync).toHaveBeenCalledTimes(1);
    expect(other.onResync).toHaveBeenCalledTimes(1);
    expect(h.onEvent).not.toHaveBeenCalled();
    t.push('a', 21, 20, 'changed', { n: 21 });
    expect(h.onEvent).toHaveBeenCalledWith({ n: 21 }, expect.anything());
  });

  it('answers a poll answer whose rev is below the held one with onResync', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 20, 19, 'changed', {});
    t.take()[0].answer({ topics: { a: { rev: 4, events: [] } } });
    await flush();
    expect(h.onResync).toHaveBeenCalledTimes(1);
    t.push('a', 5, 4, 'changed', { n: 5 });
    expect(h.onEvent).toHaveBeenCalledWith({ n: 5 }, expect.anything());
  });

  it('answers a gap inside a poll answer with onResync', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 20, 19, 'changed', {});
    t.take()[0].answer({
      topics: { a: { rev: 20, events: [{ rev: 20, prev: 19, event: 'changed', version: 1, payload: {} }] } },
    });
    await flush();
    expect(h.onResync).toHaveBeenCalledTimes(1);
    expect(h.onEvent).not.toHaveBeenCalled();
  });

  it('does not resync on events: null when pushes applied during the poll reached the answer rev', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 12, 11, 'changed', {});
    const [poll] = t.take();
    t.push('a', 11, 10, 'changed', { n: 11 });
    t.push('a', 12, 11, 'changed', { n: 12 });
    poll.answer({ topics: { a: { rev: 12, events: null } } });
    await flush();
    expect(h.onResync).not.toHaveBeenCalled();
    expect(h.onEvent).toHaveBeenCalledTimes(2);
  });

  it('applies a gap poll answer that leaves out filtered events, and polls no further', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    // Rev 11 was refused by a delivery filter: the push of 12 carries prev 11.
    t.push('a', 12, 11, 'changed', { n: 12 });
    const [poll] = t.take();
    expect(poll.body).toEqual({ cursors: { a: 10 }, scopes: {} });
    poll.answer({
      topics: { a: { rev: 12, events: [{ rev: 12, prev: 10, event: 'changed', version: 1, payload: { n: 12 } }] } },
    });
    await flush();
    expect(payloads(h)).toEqual([{ n: 12 }]);
    expect(h.onEvent.mock.calls[0][1]).toEqual({ topic: 'a', rev: 12, prev: 10, version: 1 });
    expect(h.onResync).not.toHaveBeenCalled();
    expect(t.take()).toEqual([]);
    t.push('a', 13, 12, 'changed', { n: 13 });
    expect(payloads(h)).toEqual([{ n: 12 }, { n: 13 }]);
    expect(t.take()).toEqual([]);
  });

  it('moves the held rev to the answer rev past the events a poll answer leaves out at its end', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 14, 13, 'changed', { n: 14 });
    t.take()[0].answer({
      topics: { a: { rev: 14, events: [{ rev: 11, prev: 10, event: 'changed', version: 1, payload: { n: 11 } }] } },
    });
    await flush();
    expect(payloads(h)).toEqual([{ n: 11 }]);
    expect(h.onResync).not.toHaveBeenCalled();
    expect(t.take()).toEqual([]);
    // The held rev is 14: the next push chains on it without a gap poll.
    t.push('a', 15, 14, 'changed', { n: 15 });
    expect(payloads(h)).toEqual([{ n: 11 }, { n: 15 }]);
    expect(t.take()).toEqual([]);
  });

  it('applies a polled event whose prev is below the held rev and whose rev is above it', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 13, 12, 'changed', { n: 13 });
    const [poll] = t.take();
    // The push of 11 was admitted; the poll's filters refuse 11 and 12, so its 13 follows the cursor.
    t.push('a', 11, 10, 'changed', { n: 11 });
    poll.answer({
      topics: { a: { rev: 13, events: [{ rev: 13, prev: 10, event: 'changed', version: 1, payload: { n: 13 } }] } },
    });
    await flush();
    expect(payloads(h)).toEqual([{ n: 11 }, { n: 13 }]);
    expect(h.onEvent.mock.calls[1][1]).toEqual({ topic: 'a', rev: 13, prev: 11, version: 1 });
    expect(h.onResync).not.toHaveBeenCalled();
    expect(t.take()).toEqual([]);
  });

  it('dispatches by event name, and ignores an event of a topic the client does not follow', async () => {
    const t = makeTransport();
    const { c, h } = await followed(t);
    const other = handlers();
    c.subscribe('a', Other, other);
    t.push('a', 11, 10, 'other', { x: 'y' });
    expect(other.onEvent).toHaveBeenCalledWith({ x: 'y' }, expect.anything());
    expect(h.onEvent).not.toHaveBeenCalled();
    // The held rev moved with the event the first subscription does not listen to.
    t.push('a', 12, 11, 'changed', { n: 12 });
    expect(h.onEvent).toHaveBeenCalledWith({ n: 12 }, expect.anything());

    expect(t.push('b', 1, 0, 'changed', {})).toBe(true);
    expect(t.take()).toEqual([]);
  });

  it('holds a push that arrives before the first answer and replays it against that answer', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const h = handlers();
    c.subscribe('a', Changed, h);
    t.push('a', 10, 9, 'changed', { n: 10 });
    t.push('a', 11, 10, 'changed', { n: 11 });
    expect(h.onEvent).not.toHaveBeenCalled();
    t.take()[0].answer({ rev: 10 });
    await flush();
    expect(payloads(h)).toEqual([{ n: 11 }]);
  });

  it('reports a topic error of a poll answer through onError and removes the subscriptions of the topic', async () => {
    const t = makeTransport();
    const { h, sub } = await followed(t);
    t.push('a', 20, 19, 'changed', {});
    t.take()[0].answer({ topics: { a: { rev: null, events: null, error: 'fastapi-viewsets.event_topic_refused' } } });
    await flush();
    expect(h.onError).toHaveBeenCalledTimes(1);
    const error = h.onError.mock.calls[0][0] as ViewSetRequestError;
    expect(error).toBeInstanceOf(ViewSetRequestError);
    expect(error.response).toMatchObject({
      status: 403,
      data: { detail_code: 'fastapi-viewsets.event_topic_refused' },
    });
    await expect(sub.ready).resolves.toBeUndefined();
    // The socket subscription of a refused topic is ended.
    expect(t.take()).toMatchObject([{ path: '/events/unsubscribe', body: { topic: 'a' } }]);
    // A followed topic would apply this push, at the held rev 10, and a gap push would poll.
    t.push('a', 11, 10, 'changed', {});
    t.push('a', 30, 29, 'changed', {});
    expect(h.onEvent).not.toHaveBeenCalled();
    expect(t.take()).toEqual([]);
  });

  it('stops replaying held pushes once a replayed handler ends the following of the topic', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const h = handlers();
    const sub = c.subscribe('imports:7', Changed, h);
    h.onEvent.mockImplementation(() => sub.unsubscribe());
    t.push('imports:7', 6, 5, 'changed', { n: 6 });
    t.push('imports:7', 9, 8, 'changed', { n: 9 });
    t.take()[0].answer({ rev: 5 });
    await flush();
    expect(payloads(h)).toEqual([{ n: 6 }]);
    expect(t.take().map((call) => call.path)).toEqual(['/events/unsubscribe']);
  });

  it('a handler that throws does not stop the others, and its error is rethrown in a microtask', async () => {
    const t = makeTransport();
    const { c, h } = await followed(t);
    const scheduled: (() => void)[] = [];
    const spy = vi.spyOn(globalThis, 'queueMicrotask').mockImplementation((task) => scheduled.push(task));
    try {
      const throwing = handlers();
      throwing.onEvent.mockImplementation(() => {
        throw new Error('handler');
      });
      c.subscribe('a', Changed, throwing);
      const after = handlers();
      c.subscribe('a', Changed, after);
      t.push('a', 11, 10, 'changed', { n: 11 });
      expect(h.onEvent).toHaveBeenCalledWith({ n: 11 }, expect.anything());
      expect(after.onEvent).toHaveBeenCalledWith({ n: 11 }, expect.anything());
      expect(scheduled).toHaveLength(1);
      expect(scheduled[0]).toThrow('handler');
    } finally {
      spy.mockRestore();
    }
  });
});

describe('event versions', () => {
  it("passes a push's topic, rev, prev and :version to onEvent as a frozen EventMeta", async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 11, 10, 'changed', { n: 11 }, 3);
    expect(h.onEvent).toHaveBeenCalledWith({ n: 11 }, { topic: 'a', rev: 11, prev: 10, version: 3 });
    const meta: EventMeta = h.onEvent.mock.calls[0][1];
    expect(Object.isFrozen(meta)).toBe(true);
  });

  it("passes a poll answer entry's version to onEvent", async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 12, 11, 'changed', {});
    t.take()[0].answer({
      topics: {
        a: {
          rev: 12,
          events: [
            { rev: 11, prev: 10, event: 'changed', version: 1, payload: { n: 11 } },
            { rev: 12, prev: 11, event: 'changed', version: 2, payload: { n: 12 } },
          ],
        },
      },
    });
    await flush();
    expect(h.onEvent.mock.calls).toEqual([
      [{ n: 11 }, { topic: 'a', rev: 11, prev: 10, version: 1 }],
      [{ n: 12 }, { topic: 'a', rev: 12, prev: 11, version: 2 }],
    ]);
    expect(h.onResync).not.toHaveBeenCalled();
  });

  it('dispatches every version of the event name to one subscription, which branches on meta.version', async () => {
    const t = makeTransport();
    const c = track(client(t, { axios: t.axios }));
    const ImportFinished = defineEvent<{ file: string; rows: number; skipped?: number }>('import_finished');
    const seen: string[] = [];
    const sub = c.subscribe('imports:7', ImportFinished, {
      onEvent(payload, { version }) {
        seen.push(
          version >= 2 ? `${payload.file}: ${payload.rows}, ${payload.skipped}` : `${payload.file}: ${payload.rows}`,
        );
      },
      onResync() {},
    });
    t.take()[0].answer({ rev: 10 });
    await sub.ready;
    t.push('imports:7', 11, 10, 'import_finished', { file: 'a.csv', rows: 3 }, 1);
    t.push('imports:7', 12, 11, 'import_finished', { file: 'b.csv', rows: 5, skipped: 2 }, 2);
    t.push('imports:7', 13, 12, 'import_finished', { file: 'c.csv', rows: 1, skipped: 0 }, '2');
    expect(seen).toEqual(['a.csv: 3', 'b.csv: 5, 2', 'c.csv: 1, 0']);
  });

  it('ignores a push without a usable :version, as a push without a usable rev', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    const headers = { ':event': 'changed', ':topic': 'a', ':rev': 11, ':prev': 10 };
    expect(handleEventStream(t.peer, { n: 11 }, { headers })).toBe(true);
    for (const version of [null, 0, -1, 1.5, '', 'x', '1.5', true, NaN]) {
      expect(t.push('a', 11, 10, 'changed', { n: 11 }, version)).toBe(true);
    }
    expect(h.onEvent).not.toHaveBeenCalled();
    expect(t.take()).toEqual([]);
    // The held rev is still 10.
    t.push('a', 11, 10, 'changed', { n: 11 });
    expect(h.onEvent).toHaveBeenCalledWith({ n: 11 }, { topic: 'a', rev: 11, prev: 10, version: 1 });
  });

  it('answers a poll answer entry without a usable version with onResync', async () => {
    for (const version of [undefined, 0, 2.5, 'two']) {
      const t = makeTransport();
      const { h } = await followed(t);
      t.push('a', 12, 11, 'changed', {});
      t.take()[0].answer({
        topics: {
          a: {
            rev: 12,
            events: [
              { rev: 11, prev: 10, event: 'changed', version: 1, payload: { n: 11 } },
              { rev: 12, prev: 11, event: 'changed', version, payload: { n: 12 } },
            ],
          },
        },
      });
      await flush();
      expect(payloads(h)).toEqual([{ n: 11 }]);
      expect(h.onResync).toHaveBeenCalledTimes(1);
      // The held rev is the answer's.
      t.push('a', 13, 12, 'changed', { n: 13 });
      expect(h.onEvent).toHaveBeenLastCalledWith({ n: 13 }, expect.objectContaining({ rev: 13 }));
    }
  });
});

describe('polling', () => {
  function axiosClient(t: Transport, options: Partial<EventClientOptions> = {}) {
    return track(createEventClient({ basePath: '/events', axios: t.axios, draw: () => 0.5, ...options }));
  }

  it('polls a new topic at once over axios, and ready resolves on the answer', async () => {
    const t = makeTransport();
    const c = axiosClient(t);
    const sub = c.subscribe('a', Changed, handlers());
    await flush();
    const [poll] = t.take();
    expect(poll).toMatchObject({ via: 'axios', path: '/events/poll', body: { cursors: { a: null }, scopes: {} } });
    expect(t.axios.post).toHaveBeenCalledWith('/events/poll', { cursors: { a: null }, scopes: {} });
    poll.answer({ topics: { a: { rev: 7, events: [] } }, poll_after_ms: 0 });
    await expect(sub.ready).resolves.toBeUndefined();
  });

  it('carries every followed topic in one poll call per rung', async () => {
    const t = makeTransport();
    const c = axiosClient(t);
    c.subscribe('a', Changed, handlers());
    c.subscribe('b', Changed, handlers());
    c.subscribe('a', Other, handlers());
    await flush();
    const calls = t.take();
    expect(calls).toHaveLength(1);
    expect(calls[0].body).toEqual({ cursors: { a: null, b: null }, scopes: {} });
    calls[0].answer({ topics: { a: { rev: 1, events: [] }, b: { rev: 2, events: [] } } });
    await flush();
    await vi.advanceTimersByTimeAsync(400);
    expect(t.take().map((call) => call.body)).toEqual([{ cursors: { a: 1, b: 2 }, scopes: {} }]);
  });

  it('polls from the answer rev after an answer whose events were all left out', async () => {
    const t = makeTransport();
    const c = axiosClient(t);
    const h = handlers();
    c.subscribe('a', Changed, h);
    await flush();
    t.take()[0].answer({ topics: { a: { rev: 7, events: [] } }, poll_after_ms: 0 });
    await flush();
    await vi.advanceTimersByTimeAsync(400);
    const [second] = t.take();
    expect(second.body).toEqual({ cursors: { a: 7 }, scopes: {} });
    second.answer({ topics: { a: { rev: 9, events: [] } }, poll_after_ms: 0 });
    await flush();
    // The rev moved, so the ladder stays on its 400 ms base.
    await vi.advanceTimersByTimeAsync(400);
    expect(t.take().map((call) => call.body)).toEqual([{ cursors: { a: 9 }, scopes: {} }]);
    expect(h.onEvent).not.toHaveBeenCalled();
    expect(h.onResync).not.toHaveBeenCalled();
  });

  it('climbs ×1.5 from the 400 ms base while no rev moves, and returns to the base when one does', async () => {
    const t = makeTransport();
    const h = handlers();
    axiosClient(t).subscribe('a', Changed, h);
    await flush();
    t.take()[0].answer({ topics: { a: { rev: 1, events: [] } } });
    await flush();

    const gaps: number[] = [];
    let last = Date.now();
    const rung = async (answer: unknown) => {
      await vi.advanceTimersToNextTimerAsync();
      const [call] = t.take();
      gaps.push(Date.now() - last);
      last = Date.now();
      call.answer(answer);
      await flush();
    };
    const idle = { topics: { a: { rev: 1, events: [] } } };
    await rung(idle);
    await rung(idle);
    await rung({ topics: { a: { rev: 2, events: [{ rev: 2, prev: 1, event: 'changed', version: 1, payload: {} }] } } });
    await rung({ topics: { a: { rev: 2, events: [] } } });
    expect(gaps).toEqual([400, 600, 900, 400]);
    expect(h.onEvent).toHaveBeenCalledTimes(1);
  });

  it('never polls sooner than poll_after_ms', async () => {
    const t = makeTransport();
    axiosClient(t).subscribe('a', Changed, handlers());
    await flush();
    t.take()[0].answer({ topics: { a: { rev: 1, events: [] } }, poll_after_ms: 2500 });
    await flush();
    await vi.advanceTimersByTimeAsync(2499);
    expect(t.take()).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    expect(t.take()).toHaveLength(1);
  });

  it('polls again on the next rung after a poll that failed without a status', async () => {
    const t = makeTransport();
    const sub = axiosClient(t).subscribe('a', Changed, handlers());
    await flush();
    t.take()[0].fail(new Error('Network Error'));
    await flush();
    await vi.advanceTimersByTimeAsync(400);
    const [again] = t.take();
    expect(again.body).toEqual({ cursors: { a: null }, scopes: {} });
    again.answer({ topics: { a: { rev: 1, events: [] } } });
    await expect(sub.ready).resolves.toBeUndefined();
  });

  it('polls again on the next rung after a poll answered 500, and refuses nothing', async () => {
    const t = makeTransport();
    const h = handlers();
    const sub = axiosClient(t).subscribe('a', Changed, h);
    await flush();
    t.take()[0].answer({ detail: 'server error' }, 500);
    await flush();
    expect(h.onError).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(400);
    const [again] = t.take();
    expect(again.body).toEqual({ cursors: { a: null }, scopes: {} });
    again.answer({ topics: { a: { rev: 1, events: [] } } });
    await expect(sub.ready).resolves.toBeUndefined();
  });

  it('keeps a topic answered rate_limited and polls it on the next rung', async () => {
    const t = makeTransport();
    const c = axiosClient(t);
    const h = handlers();
    c.subscribe('a', Changed, h);
    await flush();
    t.take()[0].answer({ topics: { a: { rev: 3, events: [] } } });
    await flush();
    await vi.advanceTimersByTimeAsync(400);
    t.take()[0].answer({ topics: { a: { rev: null, events: null, error: 'fastapi-viewsets.rate_limited' } } });
    await flush();
    expect(h.onError).not.toHaveBeenCalled();
    // No rev moved: the ladder climbs to 600 ms.
    await vi.advanceTimersByTimeAsync(600);
    const [again] = t.take();
    expect(again.body).toEqual({ cursors: { a: 3 }, scopes: {} });
    again.answer({
      topics: { a: { rev: 4, events: [{ rev: 4, prev: 3, event: 'changed', version: 1, payload: { n: 4 } }] } },
    });
    await flush();
    expect(h.onEvent).toHaveBeenCalledWith({ n: 4 }, expect.anything());
  });

  it('reports a topic answered session_expired with status 401 and removes it', async () => {
    const t = makeTransport();
    const c = axiosClient(t);
    const h = handlers();
    const sub = c.subscribe('a', Changed, h);
    await flush();
    t.take()[0].answer({ topics: { a: { rev: null, events: null, error: 'fastapi-viewsets.session_expired' } } });
    await expect(sub.ready).rejects.toMatchObject({
      response: { status: 401, data: { detail_code: 'fastapi-viewsets.session_expired' } },
    });
    expect(h.onError).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(t.take()).toEqual([]);
  });

  it('a poll refused with a status refuses every topic it carried', async () => {
    const t = makeTransport();
    const c = axiosClient(t);
    const h = handlers();
    const sub = c.subscribe('a', Changed, h);
    await flush();
    t.take()[0].answer({ detail: 'no' }, 401);
    await expect(sub.ready).rejects.toMatchObject({ response: { status: 401 } });
    expect(h.onError).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(t.take()).toEqual([]);
  });

  it('the last unsubscribe of a polled topic leaves it out of the next poll; no topic, no ladder', async () => {
    const t = makeTransport();
    const c = axiosClient(t);
    const a = c.subscribe('a', Changed, handlers());
    const b = c.subscribe('b', Changed, handlers());
    await flush();
    t.take()[0].answer({ topics: { a: { rev: 1, events: [] }, b: { rev: 1, events: [] } } });
    await flush();
    a.unsubscribe();
    await vi.advanceTimersByTimeAsync(400);
    const [poll] = t.take();
    expect(poll.body).toEqual({ cursors: { b: 1 }, scopes: {} });
    poll.answer({ topics: { b: { rev: 1, events: [] } } });
    await flush();
    b.unsubscribe();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(t.take()).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('polls a topic added while a poll is in flight right after that poll', async () => {
    const t = makeTransport();
    const c = axiosClient(t);
    c.subscribe('a', Changed, handlers());
    await flush();
    const [first] = t.take();
    const b = c.subscribe('b', Changed, handlers());
    first.answer({ topics: { a: { rev: 1, events: [] } } });
    await flush();
    const [second] = t.take();
    expect(second.body).toEqual({ cursors: { a: 1, b: null }, scopes: {} });
    second.answer({ topics: { a: { rev: 1, events: [] }, b: { rev: 2, events: [] } } });
    await expect(b.ready).resolves.toBeUndefined();
  });

  it('close() clears the timer', async () => {
    const t = makeTransport();
    const c = axiosClient(t);
    c.subscribe('a', Changed, handlers());
    await flush();
    t.take()[0].answer({ topics: { a: { rev: 1, events: [] } } });
    await flush();
    expect(vi.getTimerCount()).toBe(1);
    c.close();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('the peer: ladder held, lost and reconnected', () => {
  it('holds the ladder while the peer is open and every topic is subscribed', async () => {
    const t = makeTransport();
    await followed(t);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(t.take()).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('losing the peer resumes the ladder on its base rung over axios, from the held revs', async () => {
    const t = makeTransport();
    await followed(t);
    t.close();
    await vi.advanceTimersByTimeAsync(399);
    expect(t.take()).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    const [poll] = t.take();
    expect(poll).toMatchObject({ via: 'axios', path: '/events/poll', body: { cursors: { a: 10 }, scopes: {} } });
  });

  it('losing the peer while a rung polls over it resumes on the base rung', async () => {
    const t = makeTransport();
    const c = track(client(t, { axios: t.axios }));
    c.subscribe('a', Changed, handlers());
    t.take()[0].fail(new Error('no status'));
    await vi.advanceTimersByTimeAsync(400);
    t.take()[0].answer({ topics: { a: { rev: 1, events: [] } } });
    await flush();
    await vi.advanceTimersByTimeAsync(600);
    const [inFlight] = t.take();
    expect(inFlight.via).toBe('peer');
    t.close();
    inFlight.fail(Object.assign(new Error('lost'), { name: 'ConnectionLost' }));
    await flush();
    await vi.advanceTimersByTimeAsync(399);
    expect(t.take()).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    expect(t.take()).toMatchObject([{ via: 'axios', body: { cursors: { a: 1 }, scopes: {} } }]);
  });

  it('losing the peer while the ladder waits restarts it on the base rung', async () => {
    const t = makeTransport();
    const c = track(client(t, { axios: t.axios }));
    c.subscribe('a', Changed, handlers());
    t.take()[0].fail(new Error('no status'));
    await vi.advanceTimersByTimeAsync(400);
    t.take()[0].answer({ topics: { a: { rev: 1, events: [] } } });
    await flush();
    // The ladder now waits 600 ms; 300 of them pass before the peer is lost.
    await vi.advanceTimersByTimeAsync(300);
    t.close();
    await vi.advanceTimersByTimeAsync(399);
    expect(t.take()).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    expect(t.take()).toMatchObject([{ via: 'axios', body: { cursors: { a: 1 }, scopes: {} } }]);
  });

  it('drops a subscribe answer from the lost socket, and polls a topic without a rev at once', async () => {
    const t = makeTransport();
    const c = track(client(t, { axios: t.axios }));
    c.subscribe('a', Changed, handlers());
    const [subscribe] = t.take();
    t.close();
    subscribe.answer({ rev: 99 });
    await flush();
    expect(t.take()).toMatchObject([{ via: 'axios', body: { cursors: { a: null }, scopes: {} } }]);
  });

  it('a client without axios waits for the reconnect', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const sub = c.subscribe('a', Changed, handlers());
    t.take()[0].answer({ rev: 10 });
    await sub.ready;
    t.close();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(t.take()).toEqual([]);
    expect(t.axios.post).not.toHaveBeenCalled();
  });

  it('on reconnect subscribes every topic; an equal rev resumes without a read and holds the ladder', async () => {
    const t = makeTransport();
    const { c, h } = await followed(t);
    c.subscribe('b', Changed, handlers());
    t.take()[0].answer({ rev: 3 });
    await flush();
    t.close();
    await vi.advanceTimersByTimeAsync(400);
    const [poll] = t.take();
    poll.answer({ topics: { a: { rev: 10, events: [] }, b: { rev: 3, events: [] } } });
    await flush();

    t.reconnect();
    const subscribes = t.take();
    expect(subscribes.map((call) => [call.via, call.path, call.body])).toEqual([
      ['peer', '/events/subscribe', { topic: 'a' }],
      ['peer', '/events/subscribe', { topic: 'b' }],
    ]);
    subscribes[0].answer({ rev: 10 });
    subscribes[1].answer({ rev: 3 });
    await flush();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(t.take()).toEqual([]);
    expect(h.onResync).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('on reconnect a larger rev polls from the held rev', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.close();
    t.reconnect();
    t.take()[0].answer({ rev: 12 });
    await flush();
    const [poll] = t.take();
    expect(poll).toMatchObject({ via: 'peer', path: '/events/poll', body: { cursors: { a: 10 }, scopes: {} } });
    poll.answer({
      topics: {
        a: {
          rev: 12,
          events: [
            { rev: 11, prev: 10, event: 'changed', version: 1, payload: { n: 11 } },
            { rev: 12, prev: 11, event: 'changed', version: 1, payload: { n: 12 } },
          ],
        },
      },
    });
    await flush();
    expect(h.onEvent).toHaveBeenCalledTimes(2);
  });

  it('a subscribe refused on the new socket is reported through subscription.onError and removes it', async () => {
    const t = makeTransport();
    const { sub } = await followed(t);
    const onError = vi.fn();
    sub.onError = onError;
    t.close();
    t.reconnect();
    t.take()[0].answer({ detail: 'refused' }, 403);
    await flush();
    expect(onError).toHaveBeenCalledWith(
      expect.objectContaining({ response: expect.objectContaining({ status: 403 }) }),
    );
    await vi.advanceTimersByTimeAsync(60_000);
    expect(t.take()).toEqual([]);
  });

  it('polls over the peer while a subscribe is unanswered, and holds the ladder once it is answered', async () => {
    const t = makeTransport();
    const c = track(client(t));
    c.subscribe('a', Changed, handlers());
    const [subscribe] = t.take();
    await vi.advanceTimersByTimeAsync(400);
    const [poll] = t.take();
    expect(poll).toMatchObject({ via: 'peer', path: '/events/poll' });
    subscribe.answer({ rev: 1 });
    poll.answer({ topics: { a: { rev: 1, events: [] } } });
    await flush();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(t.take()).toEqual([]);
  });
});

describe('the check poll', () => {
  async function checked(t: Transport, options: Partial<EventClientOptions> = {}) {
    const c = track(client(t, { axios: t.axios, checkIntervalMs: 60_000, ...options }));
    const h = handlers();
    const sub = c.subscribe('a', Changed, h);
    t.take()[0].answer({ rev: 10 });
    await sub.ready;
    return { c, h, sub };
  }

  it('polls every subscribed topic over the open peer once per checkIntervalMs', async () => {
    const t = makeTransport();
    await checked(t);
    await vi.advanceTimersByTimeAsync(59_999);
    expect(t.take()).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    const [poll] = t.take();
    expect(poll).toMatchObject({ via: 'peer', path: '/events/poll', body: { cursors: { a: 10 }, scopes: {} } });
    poll.answer({ topics: { a: { rev: 10, events: [] } } });
    await flush();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(t.take()).toMatchObject([{ via: 'peer', body: { cursors: { a: 10 }, scopes: {} } }]);
  });

  it('applies the events a lost push left out', async () => {
    const t = makeTransport();
    const { h } = await checked(t);
    await vi.advanceTimersByTimeAsync(60_000);
    t.take()[0].answer({
      topics: { a: { rev: 11, events: [{ rev: 11, prev: 10, event: 'changed', version: 1, payload: { n: 11 } }] } },
    });
    await flush();
    expect(payloads(h)).toEqual([{ n: 11 }]);
    expect(t.push('a', 12, 11, 'changed', { n: 12 })).toBe(true);
    expect(payloads(h)).toEqual([{ n: 11 }, { n: 12 }]);
    expect(t.take()).toEqual([]);
  });

  it('jitters the interval by pollSettings.jitterFraction', async () => {
    const t = makeTransport();
    await checked(t, { draw: () => 0 });
    await vi.advanceTimersByTimeAsync(47_999);
    expect(t.take()).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    expect(t.take()).toMatchObject([{ via: 'peer', path: '/events/poll' }]);
  });

  it('a check that fails without a status leaves the topic to the ladder', async () => {
    const t = makeTransport();
    await checked(t);
    await vi.advanceTimersByTimeAsync(60_000);
    t.take()[0].fail(new Error('no status'));
    await flush();
    await vi.advanceTimersByTimeAsync(400);
    expect(t.take()).toMatchObject([{ via: 'peer', body: { cursors: { a: 10 }, scopes: {} } }]);
  });

  it('a refused check is reported through onError and removes the subscription', async () => {
    const t = makeTransport();
    const { h } = await checked(t);
    await vi.advanceTimersByTimeAsync(60_000);
    t.take()[0].answer({ detail: 'no' }, 403);
    await flush();
    expect(h.onError).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('sends nothing while the peer is closed; the ladder polls over axios', async () => {
    const t = makeTransport();
    await checked(t);
    t.close();
    await vi.advanceTimersByTimeAsync(400);
    t.take()[0].answer({ topics: { a: { rev: 10, events: [] } } });
    await flush();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(t.take().every((call) => call.via === 'axios')).toBe(true);
  });

  it('checkIntervalMs 0 or non-finite sends none, and the last unsubscribe stops the timer', async () => {
    for (const checkIntervalMs of [0, NaN, Infinity]) {
      const t = makeTransport();
      const { c } = await checked(t, { checkIntervalMs });
      expect(vi.getTimerCount()).toBe(0);
      c.close();
    }
    const t2 = makeTransport();
    const { sub } = await checked(t2);
    expect(vi.getTimerCount()).toBe(1);
    sub.unsubscribe();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('close() stops the timer', async () => {
    const t = makeTransport();
    const { c } = await checked(t);
    c.close();
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('failures and malformed arrivals', () => {
  it('a refused gap poll is reported through onError and removes the subscriptions', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 13, 12, 'changed', {});
    t.take()[0].answer({ detail: 'no' }, 403);
    await flush();
    expect(h.onError).toHaveBeenCalledTimes(1);
    t.push('a', 11, 10, 'changed', {});
    expect(h.onEvent).not.toHaveBeenCalled();
  });

  /** Answers the poll from rev 10 to rev 13 and checks that the ladder is held afterwards. */
  async function catchUp(t: Transport, h: ReturnType<typeof handlers>) {
    await vi.advanceTimersByTimeAsync(399);
    expect(t.take()).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    const [poll] = t.take();
    expect(poll).toMatchObject({ via: 'peer', path: '/events/poll', body: { cursors: { a: 10 }, scopes: {} } });
    poll.answer({
      topics: {
        a: {
          rev: 13,
          events: [
            { rev: 11, prev: 10, event: 'changed', version: 1, payload: { n: 11 } },
            { rev: 12, prev: 11, event: 'changed', version: 1, payload: { n: 12 } },
            { rev: 13, prev: 12, event: 'changed', version: 1, payload: { n: 13 } },
          ],
        },
      },
    });
    await flush();
    expect(h.onEvent).toHaveBeenCalledTimes(3);
    expect(h.onError).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(t.take()).toEqual([]);
    expect(vi.getTimerCount()).toBe(0);
  }

  it('a gap poll that fails without a status leaves the topic to the ladder over the open peer', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 13, 12, 'changed', {});
    t.take()[0].fail(new Error('no status'));
    await flush();
    await catchUp(t, h);
  });

  it('a gap poll answered 503 leaves the topic to the ladder over the open peer', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 13, 12, 'changed', {});
    t.take()[0].answer({ detail: 'unavailable' }, 503);
    await flush();
    await catchUp(t, h);
  });

  it('a gap poll whose topic is answered rate_limited leaves the topic to the ladder', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 13, 12, 'changed', {});
    t.take()[0].answer({ topics: { a: { rev: null, events: null, error: 'fastapi-viewsets.rate_limited' } } });
    await flush();
    await catchUp(t, h);
  });

  it('a gap poll whose answer leaves the topic out leaves the topic to the ladder', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 13, 12, 'changed', {});
    t.take()[0].answer({ topics: {}, poll_after_ms: 0 });
    await flush();
    await catchUp(t, h);
  });

  it('the gap poll of a reconnect subscribe answered 429 leaves the topic to the ladder', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.close();
    t.reconnect();
    t.take()[0].answer({ rev: 13 });
    await flush();
    const [gap] = t.take();
    expect(gap.body).toEqual({ cursors: { a: 10 }, scopes: {} });
    gap.answer({ detail: 'rate limited', detail_code: 'fastapi-viewsets.rate_limited' }, 429);
    await flush();
    await catchUp(t, h);
  });

  it('a subscribe refused after its subscription was removed reports nothing', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const h = handlers();
    const sub = c.subscribe('a', Changed, h);
    const [subscribe] = t.take();
    sub.unsubscribe();
    subscribe.answer({ detail: 'no' }, 403);
    await flush();
    expect(h.onError).not.toHaveBeenCalled();
    expect(t.take()).toEqual([]);
  });

  it('a topic subscribed again while its subscribe is in flight is subscribed after it', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const first = c.subscribe('a', Changed, handlers());
    const [oldSubscribe] = t.take();
    first.unsubscribe();
    const second = c.subscribe('a', Changed, handlers());
    expect(t.take()).toEqual([]);
    oldSubscribe.answer({ rev: 1 });
    await flush();
    const [newSubscribe] = t.take();
    expect(newSubscribe).toMatchObject({ path: '/events/subscribe', body: { topic: 'a' } });
    newSubscribe.answer({ rev: 1 });
    await expect(second.ready).resolves.toBeUndefined();
    expect(t.take()).toEqual([]);
  });

  it('a subscribe answered 500 leaves the topic polled over the peer, not refused', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const h = handlers();
    const sub = c.subscribe('a', Changed, h);
    t.take()[0].answer({ detail: 'server error' }, 500);
    await flush();
    expect(h.onError).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(400);
    const [poll] = t.take();
    expect(poll).toMatchObject({ via: 'peer', path: '/events/poll', body: { cursors: { a: null }, scopes: {} } });
    poll.answer({ topics: { a: { rev: 2, events: [] } } });
    await expect(sub.ready).resolves.toBeUndefined();
  });

  it.each([
    ['429', (call: SentCall) => call.answer({ detail: 'slow down' }, 429)],
    ['503', (call: SentCall) => call.answer({ detail: 'unavailable' }, 503)],
    ['no status', (call: SentCall) => call.fail(new Error('stream reset'))],
  ])('a subscribe that fails with %s is sent again on the next rung over the open peer', async (_case, failWith) => {
    const t = makeTransport();
    const c = track(client(t));
    const h = handlers();
    const sub = c.subscribe('a', Changed, h);
    failWith(t.take()[0]);
    await flush();
    await vi.advanceTimersByTimeAsync(400);
    const [poll, subscribe] = t.take();
    expect(poll).toMatchObject({ via: 'peer', path: '/events/poll' });
    expect(subscribe).toMatchObject({ via: 'peer', path: '/events/subscribe', body: { topic: 'a' } });
    subscribe.answer({ rev: 2 });
    poll.answer({ topics: { a: { rev: 2, events: [] } } });
    await expect(sub.ready).resolves.toBeUndefined();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(t.take()).toEqual([]);
    expect(h.onError).not.toHaveBeenCalled();
  });

  it('a subscribe sent again and refused is reported through onError and removes the subscription', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const h = handlers();
    const sub = c.subscribe('a', Changed, h);
    t.take()[0].answer({ detail: 'unavailable' }, 503);
    await flush();
    await vi.advanceTimersByTimeAsync(400);
    const [poll, subscribe] = t.take();
    subscribe.answer({ detail: 'refused', detail_code: 'fastapi-viewsets.event_topic_refused' }, 401);
    await expect(sub.ready).rejects.toMatchObject({ response: { status: 401 } });
    expect(h.onError).toHaveBeenCalledWith(expect.any(ViewSetRequestError));
    poll.answer({ topics: { a: { rev: 2, events: [] } } });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(t.take()).toEqual([]);
  });

  it('ignores a push without a usable rev or prev, and every push after close()', async () => {
    const t = makeTransport();
    const { c, h } = await followed(t);
    handleEventStream(
      t.peer,
      {},
      { headers: { ':event': 'changed', ':version': 1, ':topic': 'a', ':rev': 'x', ':prev': 10 } },
    );
    handleEventStream(t.peer, {}, { headers: { ':event': 'changed', ':version': 1, ':topic': 'a', ':prev': 10 } });
    expect(h.onEvent).not.toHaveBeenCalled();
    expect(t.take()).toEqual([]);
    c.close();
    expect(t.push('a', 11, 10, 'changed', {})).toBe(true);
    expect(h.onEvent).not.toHaveBeenCalled();
  });

  it('answers a malformed event in a poll answer with onResync', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 13, 12, 'changed', {});
    t.take()[0].answer({ topics: { a: { rev: 13, events: [{ rev: 11, event: 'changed', payload: {} }] } } });
    await flush();
    expect(h.onResync).toHaveBeenCalledTimes(1);
  });

  it('reports a topic error not_authorized with status 403', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 13, 12, 'changed', {});
    t.take()[0].answer({ topics: { a: { rev: null, events: null, error: 'fastapi-viewsets.not_authorized' } } });
    await flush();
    expect(h.onError.mock.calls[0][0]).toMatchObject({
      response: { status: 403, data: { detail_code: 'fastapi-viewsets.not_authorized' } },
    });
  });

  it('reports an unknown topic error code with status 403', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    t.push('a', 13, 12, 'changed', {});
    t.take()[0].answer({ topics: { a: { rev: null, events: null, error: 'unauthorized' } } });
    await flush();
    expect(h.onError.mock.calls[0][0]).toMatchObject({
      response: { status: 403, data: { detail_code: 'unauthorized' } },
    });
  });

  it('reports event_topic_invalid with status 422', async () => {
    const t = makeTransport();
    const c = track(createEventClient({ basePath: '/events', axios: t.axios }));
    const sub = c.subscribe('Bad Topic', Changed, handlers());
    await flush();
    t.take()[0].answer({
      topics: { 'Bad Topic': { rev: null, events: null, error: 'fastapi-viewsets.event_topic_invalid' } },
    });
    await expect(sub.ready).rejects.toMatchObject({ response: { status: 422 } });
  });
});

describe('unsubscribe over the peer', () => {
  it('the last subscription of a topic sends unsubscribe; an earlier one sends nothing', async () => {
    const t = makeTransport();
    const { c, sub } = await followed(t);
    const second = c.subscribe('a', Other, handlers());
    sub.unsubscribe();
    expect(t.take()).toEqual([]);
    second.unsubscribe();
    second.unsubscribe();
    const calls = t.take();
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({
      via: 'peer',
      headers: { ':method': 'POST', ':path': '/events/unsubscribe' },
      body: { topic: 'a' },
    });
  });

  it('a subscription removed before its subscribe is answered sends unsubscribe once it is', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const sub = c.subscribe('a', Changed, handlers());
    const [subscribe] = t.take();
    sub.unsubscribe();
    expect(t.take()).toEqual([]);
    subscribe.answer({ rev: 1 });
    await flush();
    expect(t.take()).toMatchObject([{ path: '/events/unsubscribe', body: { topic: 'a' } }]);
  });

  it('a topic subscribed again waits for the unsubscribe in flight', async () => {
    const t = makeTransport();
    const { c, sub } = await followed(t);
    sub.unsubscribe();
    const [unsubscribe] = t.take();
    c.subscribe('a', Changed, handlers());
    await flush();
    expect(t.take()).toEqual([]);
    unsubscribe.answer(null, 204);
    await flush();
    expect(t.take()).toMatchObject([{ path: '/events/subscribe', body: { topic: 'a' } }]);
  });

  it('close() unsubscribes every topic subscribed on the open peer', async () => {
    const t = makeTransport();
    const { c } = await followed(t);
    c.close();
    expect(t.take()).toMatchObject([{ path: '/events/unsubscribe', body: { topic: 'a' } }]);
  });

  it('close() during a subscribe in flight unsubscribes once it is answered and rejects ready', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const h = handlers();
    const sub = c.subscribe('a', Changed, h);
    const [subscribe] = t.take();
    c.close();
    await expect(sub.ready).rejects.toThrow('The event client is closed');
    expect(t.take()).toEqual([]);
    subscribe.answer({ rev: 3 });
    await flush();
    expect(t.take()).toMatchObject([{ via: 'peer', path: '/events/unsubscribe', body: { topic: 'a' } }]);
    expect(h.onError).not.toHaveBeenCalled();
    expect(h.onEvent).not.toHaveBeenCalled();
  });

  it('close() leaves a resolved ready resolved', async () => {
    const t = makeTransport();
    const { c, sub } = await followed(t);
    c.close();
    await expect(sub.ready).resolves.toBeUndefined();
  });

  it('a subscribe in flight when the client closed and the socket was lost sends no unsubscribe', async () => {
    const t = makeTransport();
    const c = track(client(t));
    c.subscribe('a', Changed, handlers());
    const [subscribe] = t.take();
    c.close();
    t.close();
    t.reconnect();
    subscribe.answer({ rev: 3 });
    await flush();
    expect(t.take()).toEqual([]);
  });

  it('close() sends no subscribe that waits for an unsubscribe in flight', async () => {
    const t = makeTransport();
    const { c, sub } = await followed(t);
    sub.unsubscribe();
    const [unsubscribe] = t.take();
    c.subscribe('a', Changed, handlers());
    c.close();
    unsubscribe.answer(null, 204);
    await flush();
    expect(t.take()).toEqual([]);
  });

  it("a new client of the peer subscribes a topic once the closed client's unsubscribe is answered", async () => {
    const t = makeTransport();
    const { c } = await followed(t);
    c.close();
    const [unsubscribe] = t.take();
    expect(unsubscribe.path).toBe('/events/unsubscribe');
    const next = track(client(t));
    next.subscribe('a', Changed, handlers());
    await flush();
    expect(t.take()).toEqual([]);
    unsubscribe.answer(null, 204);
    await flush();
    expect(t.take()).toMatchObject([{ path: '/events/subscribe', body: { topic: 'a' } }]);
  });

  it("a new client of the peer subscribes a topic after the closed client's subscribe and unsubscribe", async () => {
    const t = makeTransport();
    const c = track(client(t));
    c.subscribe('a', Changed, handlers());
    const [oldSubscribe] = t.take();
    c.close();
    const next = track(client(t));
    const sub = next.subscribe('a', Changed, handlers());
    expect(t.take()).toEqual([]);
    oldSubscribe.answer({ rev: 3 });
    await flush();
    const [unsubscribe] = t.take();
    expect(unsubscribe).toMatchObject({ path: '/events/unsubscribe', body: { topic: 'a' } });
    unsubscribe.answer(null, 204);
    await flush();
    const [subscribe] = t.take();
    expect(subscribe).toMatchObject({ path: '/events/subscribe', body: { topic: 'a' } });
    subscribe.answer({ rev: 3 });
    await expect(sub.ready).resolves.toBeUndefined();
  });
});

describe('attachEvents and handleEventStream', () => {
  it('handleEventStream returns false and does nothing for a stream without :event', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    expect(handleEventStream(t.peer, {}, { headers: { ':method': 'GET', ':path': '/x' } })).toBe(false);
    expect(h.onEvent).not.toHaveBeenCalled();
  });

  it('handleEventStream returns true for an event stream on a peer without a client', () => {
    const t = makeTransport();
    expect(handleEventStream(t.peer, {}, { headers: { ':event': 'x', ':topic': 'a', ':rev': 1, ':prev': 0 } })).toBe(
      true,
    );
  });

  it('reads revs and the version sent as strings', async () => {
    const t = makeTransport();
    const { h } = await followed(t);
    handleEventStream(
      t.peer,
      { n: 1 },
      { headers: { ':event': 'changed', ':version': '1', ':topic': 'a', ':rev': '11', ':prev': '10' } },
    );
    expect(h.onEvent).toHaveBeenCalledWith({ n: 1 }, { topic: 'a', rev: 11, prev: 10, version: 1 });
  });

  it('attachEvents dispatches an event stream to the client and passes any other stream to the fallback', async () => {
    const t = makeTransport();
    const fallback = vi.fn();
    attachEvents(t.peer, fallback);
    const { h } = await followed(t);
    await t.streamHandler!(
      { n: 11 },
      { headers: { ':event': 'changed', ':version': 1, ':topic': 'a', ':rev': 11, ':prev': 10 } },
    );
    expect(h.onEvent).toHaveBeenCalledWith({ n: 11 }, expect.anything());
    expect(fallback).not.toHaveBeenCalled();

    const other = { headers: { ':method': 'GET', ':path': '/x' } };
    await t.streamHandler!('payload', other);
    expect(fallback).toHaveBeenCalledWith('payload', other);
  });

  it('attachEvents without a fallback resets any other stream with REFUSED', async () => {
    const t = makeTransport();
    attachEvents(t.peer);
    const reset = vi.fn().mockResolvedValue(undefined);
    await t.streamHandler!(null, { headers: {}, reset });
    expect(reset).toHaveBeenCalledWith(4, expect.any(String));
  });

  it('attachEvents throws for a peer without onStream', () => {
    const t = makeTransport();
    const peer = { ...t.peer, onStream: undefined };
    expect(() => attachEvents(peer)).toThrow(/onStream/);
  });
});

describe('the axios fallback', () => {
  it('polls over axios while the peer is not open, and subscribes once it reconnects', async () => {
    const t = makeTransport();
    t.peer.isOpen = false;
    const c = track(client(t, { axios: t.axios }));
    const sub = c.subscribe('a', Changed, handlers());
    await flush();
    const [poll] = t.take();
    expect(poll.via).toBe('axios');
    poll.answer({ topics: { a: { rev: 4, events: [] } } });
    await sub.ready;
    t.reconnect();
    const [subscribe] = t.take();
    expect(subscribe).toMatchObject({ via: 'peer', path: '/events/subscribe' });
  });

  it('a gap poll that failed before the peer closed is polled on the base rung over axios', async () => {
    const t = makeTransport();
    await followed(t);
    t.push('a', 12, 11, 'changed', {});
    t.take()[0].fail(Object.assign(new Error('lost'), { name: 'ConnectionLost' }));
    t.close();
    await vi.advanceTimersByTimeAsync(400);
    expect(t.take().map((call) => [call.via, call.body])).toEqual([['axios', { cursors: { a: 10 }, scopes: {} }]]);
  });

  it('sends a gap poll over axios at once while the peer is closed', async () => {
    const t = makeTransport();
    const c = track(client(t, { axios: t.axios }));
    const h = handlers();
    c.subscribe('a', Changed, h);
    expect(t.take().map((call) => call.path)).toEqual(['/events/subscribe']);
    t.push('a', 12, 11, 'changed', { n: 12 });
    t.close();
    await flush();
    const [first] = t.take();
    expect(first).toMatchObject({ via: 'axios', body: { cursors: { a: null }, scopes: {} } });
    first.answer({ topics: { a: { rev: 10, events: [] } } });
    await flush();
    // The held push 12 (prev 11) is a gap against rev 10, and is polled without waiting for a rung.
    expect(t.take()).toMatchObject([{ via: 'axios', path: '/events/poll', body: { cursors: { a: 10 }, scopes: {} } }]);
  });
});

describe('scoped topics', () => {
  /** A client following the scoped topic `/t` at rev 10 under scope `7` over an open peer. */
  async function scoped(t: Transport, options: Partial<EventClientOptions> = {}) {
    const c = track(client(t, { axios: t.axios, ...options }));
    const h = handlers();
    const sub = c.subscribe('/t', Changed, h);
    t.take()[0].answer({ rev: 10, scope: '7' });
    await sub.ready;
    return { c, h };
  }

  it('follows the scope of the first answer and applies only the pushes of that scope', async () => {
    const t = makeTransport();
    const { h } = await scoped(t);
    t.pushScoped('/t', '8', 11, 10);
    t.push('/t', 11, 10, 'changed', { n: 0 });
    expect(h.onEvent).not.toHaveBeenCalled();
    t.pushScoped('/t', '7', 11, 10);
    expect(h.onEvent).toHaveBeenCalledWith({ n: 11 }, { topic: '/t', rev: 11, prev: 10, version: 1, scope: '7' });
  });

  it('an unscoped topic has no scope in its meta', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const h = handlers();
    c.subscribe('a', Changed, h);
    t.take()[0].answer({ rev: 1 });
    await flush();
    t.push('a', 2, 1, 'changed', { n: 2 });
    expect(h.onEvent.mock.calls[0][1]).toEqual({
      topic: 'a',
      rev: 2,
      prev: 1,
      version: 1,
    });
  });

  it('drops the pushes held before the first answer that carry another scope', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const h = handlers();
    c.subscribe('/t', Changed, h);
    t.pushScoped('/t', '8', 11, 10);
    t.pushScoped('/t', '7', 11, 10, { n: 7 });
    t.take()[0].answer({ rev: 10, scope: '7' });
    await flush();
    expect(payloads(h)).toEqual([{ n: 7 }]);
  });

  it('sends the held scopes with every poll', async () => {
    const t = makeTransport();
    const c = track(
      createEventClient({
        basePath: '/events',
        axios: t.axios,
        draw: () => 0.5,
      }),
    );
    c.subscribe('a', Changed, handlers());
    c.subscribe('/t', Changed, handlers());
    await flush();
    const [first] = t.take();
    expect(first.body).toEqual({
      cursors: { a: null, '/t': null },
      scopes: {},
    });
    first.answer({
      topics: {
        a: { rev: 1, events: [] },
        '/t': { rev: 5, events: [], scope: '7' },
      },
      poll_after_ms: 0,
    });
    await vi.advanceTimersByTimeAsync(10000);
    const [second] = t.take();
    expect(second.body).toEqual({
      cursors: { a: 1, '/t': 5 },
      scopes: { '/t': '7' },
    });
  });

  it('a poll answer under another scope resyncs the topic and subscribes it again', async () => {
    const t = makeTransport();
    const { h } = await scoped(t);
    t.pushScoped('/t', '7', 15, 12);
    const [gap] = t.take();
    expect(gap.body).toEqual({ cursors: { '/t': 10 }, scopes: { '/t': '7' } });
    gap.answer({
      topics: { '/t': { rev: 20, events: null, scope: '8' } },
      poll_after_ms: 0,
    });
    await flush();
    expect(h.onResync).toHaveBeenCalledTimes(1);
    const [subscribe] = t.take();
    expect(subscribe).toMatchObject({
      via: 'peer',
      path: '/events/subscribe',
      body: { topic: '/t' },
    });
    subscribe.answer({ rev: 20, scope: '8' });
    await flush();
    t.pushScoped('/t', '7', 21, 20);
    t.pushScoped('/t', '8', 21, 20);
    expect(h.onEvent).toHaveBeenCalledTimes(1);
    expect(h.onEvent.mock.calls[0][1]).toMatchObject({ rev: 21, scope: '8' });
  });

  it('refresh() holds the pushes until the subscribe answers; a new scope applies them after the resync', async () => {
    const t = makeTransport();
    const { c, h } = await scoped(t);
    c.refresh?.('/t');
    const [subscribe] = t.take();
    expect(subscribe.path).toBe('/events/subscribe');
    t.pushScoped('/t', '7', 11, 10);
    t.pushScoped('/t', '8', 32, 31);
    t.pushScoped('/t', '8', 31, 30);
    t.pushScoped('/t', '8', 29, 28);
    expect(h.onEvent).not.toHaveBeenCalled();
    subscribe.answer({ rev: 30, scope: '8' });
    await flush();
    expect(h.onResync).toHaveBeenCalledTimes(1);
    expect(h.onEvent.mock.calls.map(([, meta]) => [meta.rev, meta.scope])).toEqual([
      [31, '8'],
      [32, '8'],
    ]);
    expect(h.onResync.mock.invocationCallOrder[0]).toBeLessThan(h.onEvent.mock.invocationCallOrder[0]);
  });

  it('refresh() answered under the same scope applies the held pushes', async () => {
    const t = makeTransport();
    const { c, h } = await scoped(t);
    c.refresh?.();
    const [subscribe] = t.take();
    t.pushScoped('/t', '7', 11, 10);
    expect(h.onEvent).not.toHaveBeenCalled();
    subscribe.answer({ rev: 10, scope: '7' });
    await flush();
    expect(payloads(h)).toEqual([{ n: 11 }]);
    expect(h.onResync).not.toHaveBeenCalled();
  });

  it('a refresh whose subscribe fails without a refusal drops the held pushes and polls the topic', async () => {
    const t = makeTransport();
    const { c, h } = await scoped(t);
    c.refresh?.('/t');
    const [subscribe] = t.take();
    t.pushScoped('/t', '7', 11, 10);
    subscribe.fail(Object.assign(new Error('lost'), { name: 'ConnectionLost' }));
    await flush();
    await vi.advanceTimersByTimeAsync(400);
    const [poll] = t.take();
    expect(poll.body).toEqual({ cursors: { '/t': 10 }, scopes: { '/t': '7' } });
    expect(h.onEvent).not.toHaveBeenCalled();
  });

  it('a refresh whose subscribe is answered 503 keeps the subscription and subscribes again', async () => {
    const t = makeTransport();
    const { c, h } = await scoped(t);
    c.refresh?.('/t');
    t.take()[0].answer({ detail: 'unavailable' }, 503);
    await flush();
    await vi.advanceTimersByTimeAsync(400);
    const [poll, subscribe] = t.take();
    expect(poll.body).toEqual({ cursors: { '/t': 10 }, scopes: { '/t': '7' } });
    expect(subscribe).toMatchObject({ path: '/events/subscribe', body: { topic: '/t' } });
    subscribe.answer({ rev: 10, scope: '7' });
    poll.answer({ topics: { '/t': { rev: 10, events: [], scope: '7' } } });
    await flush();
    t.pushScoped('/t', '7', 11, 10);
    expect(payloads(h)).toEqual([{ n: 11 }]);
    expect(h.onError).not.toHaveBeenCalled();
  });

  it('refresh() without an open peer polls at once', async () => {
    const t = makeTransport();
    const c = track(
      createEventClient({
        basePath: '/events',
        axios: t.axios,
        draw: () => 0.5,
      }),
    );
    const h = handlers();
    c.subscribe('/t', Changed, h);
    await flush();
    t.take()[0].answer({
      topics: { '/t': { rev: 5, events: [], scope: '7' } },
      poll_after_ms: 0,
    });
    await flush();
    c.refresh?.();
    await flush();
    const [poll] = t.take();
    expect(poll.body).toEqual({ cursors: { '/t': 5 }, scopes: { '/t': '7' } });
    poll.answer({
      topics: { '/t': { rev: 9, events: null, scope: '8' } },
      poll_after_ms: 0,
    });
    await flush();
    expect(h.onResync).toHaveBeenCalledTimes(1);
  });

  it('a push after headers() changed refreshes the topic with the new headers', async () => {
    const t = makeTransport();
    let headers: Record<string, string> = { 'X-Org': '7' };
    const c = track(client(t, { headers: () => headers }));
    const h = handlers();
    c.subscribe('/t', Changed, h);
    const [first] = t.take();
    expect(first.headers).toMatchObject({
      'X-Org': '7',
      ':method': 'POST',
      ':path': '/events/subscribe',
    });
    first.answer({ rev: 10, scope: '7' });
    await flush();
    headers = { 'x-org': '7' };
    t.pushScoped('/t', '7', 11, 10);
    expect(payloads(h)).toEqual([{ n: 11 }]);
    expect(t.take()).toEqual([]);

    headers = { 'X-Org': '8' };
    t.pushScoped('/t', '7', 12, 11);
    const [again] = t.take();
    expect(again).toMatchObject({
      path: '/events/subscribe',
      headers: { 'X-Org': '8' },
    });
    again.answer({ rev: 40, scope: '8' });
    await flush();
    expect(payloads(h)).toEqual([{ n: 11 }]);
    expect(h.onResync).toHaveBeenCalledTimes(1);
  });

  it('refresh() before the first answer subscribes the topic again once it arrives', async () => {
    const t = makeTransport();
    const c = track(client(t));
    const h = handlers();
    const sub = c.subscribe('/t', Changed, h);
    const [first] = t.take();
    c.refresh?.('/t');
    expect(t.take()).toEqual([]);
    first.answer({ rev: 10, scope: '7' });
    await flush();
    await expect(sub.ready).resolves.toBeUndefined();
    const [again] = t.take();
    expect(again).toMatchObject({ path: '/events/subscribe', body: { topic: '/t' } });
    again.answer({ rev: 20, scope: '8' });
    await flush();
    expect(h.onResync).toHaveBeenCalledTimes(1);
    expect(t.take()).toEqual([]);
  });

  it('a first answer sent before headers() changed refreshes a scoped topic with the new headers', async () => {
    const t = makeTransport();
    let headers: Record<string, string> = { 'X-Org': '7' };
    const c = track(client(t, { headers: () => headers }));
    const h = handlers();
    c.subscribe('/t', Changed, h);
    c.subscribe('/u', Changed, handlers());
    const [first, unscoped] = t.take();
    headers = { 'X-Org': '8' };
    first.answer({ rev: 10, scope: '7' });
    unscoped.answer({ rev: 3 });
    await flush();
    const [again] = t.take();
    expect(again).toMatchObject({
      path: '/events/subscribe',
      body: { topic: '/t' },
      headers: { 'X-Org': '8' },
    });
    again.answer({ rev: 40, scope: '8' });
    await flush();
    expect(h.onResync).toHaveBeenCalledTimes(1);
    expect(t.take()).toEqual([]);
  });

  it('a held push and a headers() change before the first answer refresh the topic once', async () => {
    const t = makeTransport();
    let headers: Record<string, string> = { 'X-Org': '7' };
    const c = track(client(t, { headers: () => headers }));
    c.subscribe('/t', Changed, handlers());
    const [first] = t.take();
    t.pushScoped('/t', '7', 11, 10);
    headers = { 'X-Org': '8' };
    first.answer({ rev: 10, scope: '7' });
    await flush();
    const [again] = t.take();
    again.answer({ rev: 30, scope: '8' });
    await flush();
    expect(t.take()).toEqual([]);
  });

  it('a first poll answer sent before headers() changed polls a scoped topic again at once', async () => {
    const t = makeTransport();
    let headers: Record<string, string> = { 'X-Org': '7' };
    const c = track(
      createEventClient({
        basePath: '/events',
        axios: t.axios,
        draw: () => 0.5,
        headers: () => headers,
      }),
    );
    const h = handlers();
    c.subscribe('/t', Changed, h);
    await flush();
    const [first] = t.take();
    headers = { 'X-Org': '8' };
    first.answer({ topics: { '/t': { rev: 5, events: [], scope: '7' } }, poll_after_ms: 0 });
    await flush();
    const [again] = t.take();
    expect(again.headers).toMatchObject({ 'X-Org': '8' });
    expect(again.body).toEqual({ cursors: { '/t': 5 }, scopes: { '/t': '7' } });
    again.fail(new Error('network'));
    await flush();
    expect(t.take()).toEqual([]);
  });

  it('sends the headers with every call over axios, and none without the option', async () => {
    const t = makeTransport();
    const c = track(
      createEventClient({
        basePath: '/events',
        axios: t.axios,
        draw: () => 0.5,
        headers: { 'X-Org': '7' },
      }),
    );
    c.subscribe('a', Changed, handlers());
    await flush();
    expect(t.axios.post).toHaveBeenCalledWith(
      '/events/poll',
      { cursors: { a: null }, scopes: {} },
      {
        headers: { 'X-Org': '7' },
      },
    );
  });

  it('classifies a topic error by its status: event_scope_missing refuses, 503 is transient', async () => {
    const t = makeTransport();
    const c = track(
      createEventClient({
        basePath: '/events',
        axios: t.axios,
        draw: () => 0.5,
      }),
    );
    const missing = handlers();
    const unavailable = handlers();
    const unauthorized = handlers();
    const subMissing = c.subscribe('/m', Changed, missing);
    c.subscribe('/u', Changed, unavailable);
    const subUnauthorized = c.subscribe('/a', Changed, unauthorized);
    await flush();
    t.take()[0].answer({
      topics: {
        '/m': { rev: null, events: null, error: 'fastapi-viewsets.event_scope_missing' },
        '/u': {
          rev: null,
          events: null,
          error: 'fastapi-viewsets.event_topic_refused',
          status: 503,
        },
        '/a': {
          rev: null,
          events: null,
          error: 'fastapi-viewsets.event_topic_refused',
          status: 401,
        },
      },
      poll_after_ms: 0,
    });
    await expect(subMissing.ready).rejects.toMatchObject({
      response: { status: 403 },
    });
    await expect(subUnauthorized.ready).rejects.toMatchObject({
      response: { status: 401 },
    });
    expect(missing.onError).toHaveBeenCalledWith(expect.any(ViewSetRequestError));
    expect(unavailable.onError).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(10000);
    expect(t.take()[0].body).toEqual({ cursors: { '/u': null }, scopes: {} });
  });
});

describe('peer source', () => {
  /** Peers handed out one at a time; `next` is the transport the source returns next. */
  function source() {
    const made: Transport[] = [];
    const fn = vi.fn(() => {
      const t = makeTransport();
      made.push(t);
      return t.peer;
    });
    return { fn, made };
  }

  it('calls the source on the first subscription and subscribes on the peer it returns', async () => {
    const { fn, made } = source();
    const c = track(createEventClient({ basePath: '/events', peer: fn, draw: () => 0.5, checkIntervalMs: 0 }));
    expect(fn).not.toHaveBeenCalled();
    const h = handlers();
    const sub = c.subscribe('/t', Changed, h);
    c.subscribe('/u', Changed, handlers());
    await flush();
    expect(fn).toHaveBeenCalledTimes(1);
    const [t] = made;
    expect(eventsOf(t.peer)).toBe(c);
    const [first, second] = t.take();
    expect([first.path, first.body, second.body]).toEqual(['/events/subscribe', { topic: '/t' }, { topic: '/u' }]);
    first.answer({ rev: 10 });
    await expect(sub.ready).resolves.toBeUndefined();
    t.push('/t', 11, 10, 'changed', { n: 11 });
    expect(payloads(h)).toEqual([{ n: 11 }]);
  });

  it('polls over axios after the peer closes for good and moves every topic to the next peer', async () => {
    const { fn, made } = source();
    const http = makeTransport();
    const c = track(
      createEventClient({ basePath: '/events', peer: fn, axios: http.axios, draw: () => 0.5, checkIntervalMs: 0 }),
    );
    const h = handlers();
    c.subscribe('/t', Changed, h);
    await flush();
    const [first] = made;
    const [subscribed, polled] = first.take();
    subscribed.answer({ rev: 10 });
    polled.answer({ topics: { '/t': { rev: 10, events: [] } }, poll_after_ms: 0 });
    await flush();

    first.closeForGood();
    expect(eventsOf(first.peer)).toBeUndefined();
    await vi.advanceTimersByTimeAsync(60000);
    const [poll] = http.take();
    expect(poll).toMatchObject({ via: 'axios', path: '/events/poll', body: { cursors: { '/t': 10 }, scopes: {} } });
    expect(fn).toHaveBeenCalledTimes(2);
    const second = made[1];
    expect(eventsOf(second.peer)).toBe(c);
    const [subscribe] = second.take();
    expect(subscribe.body).toEqual({ topic: '/t' });
    subscribe.answer({ rev: 10 });
    poll.answer({ topics: { '/t': { rev: 10, events: [] } }, poll_after_ms: 0 });
    await flush();

    first.reconnect();
    first.push('/t', 11, 10, 'changed', { n: 0 });
    expect(first.take()).toEqual([]);
    second.push('/t', 11, 10, 'changed', { n: 11 });
    expect(payloads(h)).toEqual([{ n: 11 }]);
    expect(h.onResync).not.toHaveBeenCalled();
  });

  it('does not adopt again a peer that closed for good, and calls the source on a later rung', async () => {
    const dead = makeTransport();
    const live = makeTransport();
    const fn = vi.fn(() => (fn.mock.calls.length <= 2 ? dead.peer : live.peer));
    const c = track(createEventClient({ basePath: '/events', peer: fn, draw: () => 0.5, checkIntervalMs: 0 }));
    c.subscribe('/t', Changed, handlers());
    await flush();
    expect(eventsOf(dead.peer)).toBe(c);
    dead.take();
    dead.closeForGood();
    for (let i = 0; i < 20 && fn.mock.calls.length < 3; i += 1) await vi.advanceTimersByTimeAsync(10000);
    expect(fn).toHaveBeenCalledTimes(3);
    expect(eventsOf(dead.peer)).toBeUndefined();
    expect(eventsOf(live.peer)).toBe(c);
    expect(dead.take()).toEqual([]);
    expect(live.take().filter((call) => call.path === '/events/subscribe')).toHaveLength(1);
  });

  it('does not adopt a peer whose isClosed is true', async () => {
    const dead = makeTransport();
    const live = makeTransport();
    Object.assign(dead.peer, { isOpen: false, isClosed: true });
    const fn = vi.fn(() => (fn.mock.calls.length === 1 ? dead.peer : live.peer));
    const c = track(createEventClient({ basePath: '/events', peer: fn, draw: () => 0.5, checkIntervalMs: 0 }));
    c.subscribe('/t', Changed, handlers());
    for (let i = 0; i < 20 && fn.mock.calls.length < 2; i += 1) await vi.advanceTimersByTimeAsync(10000);
    expect(eventsOf(dead.peer)).toBeUndefined();
    expect(eventsOf(live.peer)).toBe(c);
  });

  it('keeps the peer through a close it retries', async () => {
    const { fn, made } = source();
    const c = track(createEventClient({ basePath: '/events', peer: fn, draw: () => 0.5, checkIntervalMs: 0 }));
    c.subscribe('/t', Changed, handlers());
    await flush();
    const [t] = made;
    t.take()[0].answer({ rev: 10 });
    await flush();
    t.close();
    await vi.advanceTimersByTimeAsync(60000);
    t.reconnect();
    expect(fn).toHaveBeenCalledTimes(1);
    expect(t.take().map((call) => call.body)).toEqual([{ topic: '/t' }]);
  });

  it('calls a source that rejected or threw again on a later rung, also without axios', async () => {
    let attempt = 0;
    const t = makeTransport();
    const fn = vi.fn(() => {
      attempt += 1;
      if (attempt === 1) throw new Error('not yet');
      if (attempt === 2) return Promise.reject(new Error('not yet'));
      return Promise.resolve(t.peer);
    });
    const c = track(createEventClient({ basePath: '/events', peer: fn, draw: () => 0.5, checkIntervalMs: 0 }));
    c.subscribe('/t', Changed, handlers());
    await flush();
    expect(fn).toHaveBeenCalledTimes(1);
    for (let i = 0; i < 20 && fn.mock.calls.length < 3; i += 1) await vi.advanceTimersByTimeAsync(10000);
    expect(fn).toHaveBeenCalledTimes(3);
    expect(
      t
        .take()
        .filter((call) => call.path === '/events/subscribe')
        .map((call) => call.body),
    ).toEqual([{ topic: '/t' }]);
    expect(eventsOf(t.peer)).toBe(c);
  });

  it('does not adopt a peer that has another client', async () => {
    const t = makeTransport();
    const other = track(client(t));
    const thrown: unknown[] = [];
    const queue = vi.spyOn(globalThis, 'queueMicrotask').mockImplementation((task) => {
      try {
        task();
      } catch (error) {
        thrown.push(error);
      }
    });
    try {
      const c = track(createEventClient({ basePath: '/events', peer: () => t.peer, draw: () => 0.5 }));
      c.subscribe('/t', Changed, handlers());
      await flush();
    } finally {
      queue.mockRestore();
    }
    expect(thrown.length).toBeGreaterThan(0);
    for (const error of thrown) expect(error).toEqual(new Error('This peer already has an event client'));
    expect(eventsOf(t.peer)).toBe(other);
    expect(t.take()).toEqual([]);
  });

  it('close() unregisters the client from the peer it adopted', async () => {
    const { fn, made } = source();
    const c = createEventClient({ basePath: '/events', peer: fn, draw: () => 0.5, checkIntervalMs: 0 });
    c.subscribe('/t', Changed, handlers());
    await flush();
    c.close();
    expect(eventsOf(made[0].peer)).toBeUndefined();
  });
});
