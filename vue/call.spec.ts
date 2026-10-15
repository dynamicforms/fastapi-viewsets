/**
 * Unit tests for call.ts: the key, ViewSetCall.key, and the repeat loop. The transports' use of them
 * is covered in rest-proxy.spec.ts and muxws-proxy.spec.ts.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  type AttemptFailure,
  bindCallKeys,
  type CallAttempt,
  idempotencyKeyHeader,
  issueCall,
  newCallKey,
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
