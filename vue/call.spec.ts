/**
 * Unit tests for call.ts: the key, ViewSetCall.key, the repeat loop and the call activity. The transports' use of them
 * is covered in rest-proxy.spec.ts and muxws-proxy.spec.ts.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  type AttemptFailure,
  bindCallKeys,
  type CallAttempt,
  type CallEvent,
  idempotencyKeyHeader,
  issueCall,
  newCallKey,
  onCallActivity,
  pendingCallCount,
  pendingCalls,
  retryNow,
  runCall,
} from './call';

const { crypto } = globalThis;

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

describe('newCallKey', () => {
  it('returns a fresh v4 UUID per call', () => {
    const a = newCallKey();
    expect(a).toMatch(UUID_V4);
    expect(newCallKey()).not.toBe(a);
  });

  it('builds a v4 UUID from getRandomValues where randomUUID is missing (an insecure context)', () => {
    const original = Object.getOwnPropertyDescriptor(crypto, 'randomUUID');
    Object.defineProperty(crypto, 'randomUUID', { value: undefined, configurable: true });
    try {
      expect(newCallKey()).toMatch(UUID_V4);
    } finally {
      if (original) Object.defineProperty(crypto, 'randomUUID', original);
      else delete (crypto as { randomUUID?: unknown }).randomUUID;
    }
  });
});

describe('idempotencyKeyHeader', () => {
  it('quotes the key as a Structured Field string', () => {
    expect(idempotencyKeyHeader('8e03978e-40d5-43e8-bc93-6894a57f9324')).toBe('"8e03978e-40d5-43e8-bc93-6894a57f9324"');
  });

  it('escapes " and \\', () => {
    expect(idempotencyKeyHeader('a"b\\c')).toBe('"a\\"b\\\\c"');
  });

  it.each(['', 'ключ', 'a\nb'])('rejects %j, which an sf-string cannot carry', (key) => {
    expect(() => idempotencyKeyHeader(key)).toThrow(TypeError);
  });
});

describe('bindCallKeys', () => {
  class Api {
    request(): Promise<number> {
      return issueCall(Promise.resolve(1), 'k1');
    }

    async direct(): Promise<number> {
      return this.request();
    }

    async nested(): Promise<number> {
      return this.direct();
    }

    async awaitsFirst(): Promise<number> {
      await Promise.resolve();
      return this.request();
    }

    sync(): number {
      return 2;
    }
  }

  it('sets the key of the first request a method issues on the promise it returns', async () => {
    const api = new Api();
    bindCallKeys(api, Object.prototype);
    const call = api.direct() as Promise<number> & { key?: string };
    expect(call.key).toBe('k1');
    await expect(call).resolves.toBe(1);
  });

  it('reports a nested call outward', () => {
    const api = new Api();
    bindCallKeys(api, Object.prototype);
    expect((api.nested() as Promise<number> & { key?: string }).key).toBe('k1');
  });

  it('leaves a method that awaits before its first request without a key', async () => {
    const api = new Api();
    bindCallKeys(api, Object.prototype);
    const call = api.awaitsFirst();
    expect('key' in call).toBe(false);
    await call;
  });

  it('leaves a method that returns no promise alone', () => {
    const api = new Api();
    bindCallKeys(api, Object.prototype);
    expect(api.sync()).toBe(2);
  });
});

describe('runCall', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** Attempts that fail with the queued errors in order, then succeed with 'ok'. */
  function attempts(errors: unknown[]) {
    const started: number[] = [];
    const abandoned: number[] = [];
    const start = (): CallAttempt<string> => {
      const index = started.length;
      started.push(Date.now());
      const error = errors[index];
      return {
        result: error === undefined ? Promise.resolve('ok') : Promise.reject(error),
        abandon: () => abandoned.push(index),
      };
    };
    return { start, started, abandoned };
  }

  const classify = (error: unknown): AttemptFailure => (error as { kind: AttemptFailure }).kind;
  const lost = { kind: 'lost' as const };
  const timeout = { kind: 'timeout' as const };
  const answer = { kind: null };

  it('starts the first attempt synchronously', () => {
    const { start, started } = attempts([]);
    void runCall(start, classify, undefined);
    expect(started).toHaveLength(1);
  });

  it('repeats after the delay, doubling it, and abandons each repeated attempt', async () => {
    const { start, started, abandoned } = attempts([lost, lost, lost]);
    const call = runCall(start, classify, { delayMs: 100 });
    await vi.runAllTimersAsync();
    await expect(call).resolves.toBe('ok');
    expect(started.map((at) => at - started[0])).toEqual([0, 100, 300, 700]);
    expect(abandoned).toEqual([0, 1, 2]);
  });

  it('caps the delay at 30 s', async () => {
    const { start, started } = attempts([lost, lost, lost]);
    const call = runCall(start, classify, { delayMs: 20_000 });
    await vi.runAllTimersAsync();
    await call;
    expect(started.map((at) => at - started[0])).toEqual([0, 20_000, 50_000, 80_000]);
  });

  it('repeats at once after an attempt timeout', async () => {
    const { start, started } = attempts([timeout, timeout]);
    const call = runCall(start, classify, { delayMs: 100 });
    await vi.runAllTimersAsync();
    await call;
    expect(started.map((at) => at - started[0])).toEqual([0, 0, 0]);
  });

  it('settles with an answer, without repeating', async () => {
    const { start, started } = attempts([answer]);
    await expect(runCall(start, classify, undefined)).rejects.toBe(answer);
    expect(started).toHaveLength(1);
  });

  it('rejects with the last error once withinMs has elapsed', async () => {
    const { start, started } = attempts([lost, lost, lost, lost]);
    const call = runCall(start, classify, { withinMs: 250, delayMs: 100 });
    const settled = expect(call).rejects.toBe(lost);
    await vi.runAllTimersAsync();
    await settled;
    expect(started).toHaveLength(2);
  });

  it('does not repeat with withinMs 0', async () => {
    const { start, started } = attempts([timeout]);
    await expect(runCall(start, classify, { withinMs: 0 })).rejects.toBe(timeout);
    expect(started).toHaveLength(1);
  });

  it('waits in beforeRepeat before each repeat', async () => {
    const { start, started } = attempts([lost]);
    const waited: number[] = [];
    const call = runCall(start, classify, { delayMs: 10 }, async (deadline) => {
      waited.push(deadline);
      await new Promise((resolve) => {
        setTimeout(resolve, 500);
      });
    });
    await vi.runAllTimersAsync();
    await call;
    expect(waited).toHaveLength(1);
    expect(started[1] - started[0]).toBe(510);
  });

  it('counts a start() that throws as a failed attempt', async () => {
    let calls = 0;
    const call = runCall(
      () => {
        calls += 1;
        if (calls === 1) throw lost;
        return { result: Promise.resolve('ok'), abandon: () => {} };
      },
      classify,
      { delayMs: 10 },
    );
    await vi.runAllTimersAsync();
    await expect(call).resolves.toBe('ok');
    expect(calls).toBe(2);
  });
});

describe('onCallActivity', () => {
  let events: CallEvent[];
  let stop: () => void;

  beforeEach(() => {
    vi.useFakeTimers();
    events = [];
    stop = onCallActivity((event) => events.push(event));
  });

  afterEach(() => {
    stop();
    vi.useRealTimers();
  });

  const lost = { kind: 'lost' as const };
  const timeout = { kind: 'timeout' as const };
  const classify = (error: unknown): AttemptFailure => (error as { kind: AttemptFailure }).kind;

  function failing(errors: unknown[]): () => CallAttempt<string> {
    let index = 0;
    return () => {
      const error = errors[index];
      index += 1;
      return { result: error === undefined ? Promise.resolve('ok') : Promise.reject(error), abandon: () => {} };
    };
  }

  it('reports start at once and settle when the call settles', async () => {
    const call = issueCall(Promise.resolve('ok'), 'k1');
    expect(events).toEqual([{ type: 'start', key: 'k1', background: false }]);
    await call;
    await Promise.resolve();
    expect(events.at(-1)).toEqual({ type: 'settle', key: 'k1', ok: true });
  });

  it('reports a rejected call as settled not ok, and passes background on', async () => {
    const call = issueCall(Promise.reject(new Error('no')), 'k2', true);
    await expect(call).rejects.toThrow('no');
    await Promise.resolve();
    expect(events).toEqual([
      { type: 'start', key: 'k2', background: true },
      { type: 'settle', key: 'k2', ok: false },
    ]);
  });

  it('reports every repeat with the attempt that ended and the delay before the next', async () => {
    const call = runCall(failing([lost, timeout, lost]), classify, { delayMs: 100 }, undefined, 'k3');
    await vi.runAllTimersAsync();
    await call;
    expect(events).toEqual([
      { type: 'retry', key: 'k3', failure: 'lost', attempt: 1, delayMs: 100 },
      { type: 'retry', key: 'k3', failure: 'timeout', attempt: 2, delayMs: 0 },
      { type: 'retry', key: 'k3', failure: 'lost', attempt: 3, delayMs: 200 },
    ]);
  });

  it('isolates a listener that throws', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    const throwing = onCallActivity(() => {
      throw new Error('listener');
    });
    try {
      await issueCall(Promise.resolve('ok'), 'k4');
      expect(events[0]).toEqual({ type: 'start', key: 'k4', background: false });
      expect(error).toHaveBeenCalled();
    } finally {
      throwing();
      error.mockRestore();
    }
  });

  it('stops calling a listener once it is removed', () => {
    stop();
    issueCall(Promise.resolve('ok'), 'k5');
    expect(events).toEqual([]);
  });
});

describe('retryNow', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('sends a waiting repeat at once and starts the next delay from delayMs again', async () => {
    const started: number[] = [];
    const lost = { kind: 'lost' as const };
    const errors = [lost, lost, lost, lost];
    const call = runCall(
      () => {
        const error = errors[started.length];
        started.push(Date.now());
        return { result: error === undefined ? Promise.resolve('ok') : Promise.reject(error), abandon: () => {} };
      },
      (error) => (error as { kind: AttemptFailure }).kind,
      { delayMs: 1000 },
    );
    await vi.advanceTimersByTimeAsync(1000 + 2000);
    expect(started.map((at) => at - started[0])).toEqual([0, 1000, 3000]);
    await vi.advanceTimersByTimeAsync(500);
    retryNow();
    await vi.advanceTimersByTimeAsync(0);
    expect(started.map((at) => at - started[0])).toEqual([0, 1000, 3000, 3500]);
    await vi.advanceTimersByTimeAsync(1000);
    await expect(call).resolves.toBe('ok');
    expect(started.map((at) => at - started[0])).toEqual([0, 1000, 3000, 3500, 4500]);
  });
});

describe('pendingCalls', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('lists the unsettled calls oldest first, with their repeats, and counts them', async () => {
    vi.setSystemTime(1000);
    let settleFirst!: (value: string) => void;
    const first = issueCall(new Promise<string>((resolve) => (settleFirst = resolve)), 'p1');
    vi.setSystemTime(2000);
    const lost = { kind: 'lost' as const };
    const errors = [lost];
    const second = issueCall(
      runCall(
        () => ({
          result: errors.length ? Promise.reject(errors.shift()) : new Promise<string>(() => {}),
          abandon: () => {},
        }),
        (error) => (error as { kind: AttemptFailure }).kind,
        { delayMs: 10 },
        undefined,
        'p2',
      ),
      'p2',
    );
    issueCall(new Promise<string>(() => {}), 'p3', true);
    await vi.advanceTimersByTimeAsync(0);

    expect(pendingCalls()).toEqual([
      { key: 'p1', background: false, startedAt: 1000, unanswered: 0 },
      { key: 'p2', background: false, startedAt: 2000, unanswered: 1 },
      { key: 'p3', background: true, startedAt: 2000, unanswered: 0 },
    ]);
    expect(pendingCallCount()).toBe(2);
    expect(pendingCallCount(true)).toBe(3);

    settleFirst('ok');
    await first;
    await vi.advanceTimersByTimeAsync(0);
    expect(pendingCalls().map((call) => call.key)).toEqual(['p2', 'p3']);
    void second;
  });
});
