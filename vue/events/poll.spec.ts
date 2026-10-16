/**
 * Unit tests for poll.ts: the ladder, the server's floor, the jitter and the `PollLadder` state.
 */

import { describe, expect, it } from 'vitest';

import {
  DEFAULT_POLL_SETTINGS,
  jitterMultiplier,
  ladderInterval,
  nextDelay,
  PollLadder,
  type PollLadderState,
} from './poll';

const { baseIntervalMs: BASE, ceilingMs: CEILING, backoffFactor: FACTOR } = DEFAULT_POLL_SETTINGS;

/** The middle of the jitter window: multiplier exactly 1, so the ladder is readable in the assertion. */
const centre = (): number => 0.5;

/** The lowest draw: multiplier 0.8, the one that must never undercut a floor. */
const low = (): number => 0;

/** The highest draw: multiplier just under 1.2. */
const high = (): number => 1 - Number.EPSILON;

function state(over: Partial<PollLadderState> = {}): PollLadderState {
  return { anyActive: true, anyDialogOpen: false, revChanged: false, previousDelay: null, ...over };
}

const running = { anyActive: true, anyDialogOpen: false, revChanged: false };
const idle = { anyActive: false, anyDialogOpen: false, revChanged: false };

describe('the ladder while something is active', () => {
  it('polls at the base 400 ms on the first poll and after a change', () => {
    [state(), state({ revChanged: true, previousDelay: CEILING })].forEach((first) => {
      expect(ladderInterval(first)).toBe(BASE);
      expect(nextDelay(first, centre)).toBe(BASE);
    });
    expect(BASE).toBe(400);
  });

  it('multiplies by 1.5 up to the 5 s ceiling when no rev changed', () => {
    const ladder = new PollLadder(DEFAULT_POLL_SETTINGS, centre);

    const walked = Array.from({ length: 12 }, () => ladder.next(running));
    expect(walked.slice(0, 8)).toEqual([400, 600, 900, 1350, 2025, 3038, 4556, 5000]);
    // The climb stops at the ceiling, however long nothing changes.
    expect(walked.slice(8)).toEqual([5000, 5000, 5000, 5000]);
    expect(FACTOR).toBe(1.5);
    expect(CEILING).toBe(5000);
  });

  it('resets to the base interval on any change', () => {
    const ladder = new PollLadder(DEFAULT_POLL_SETTINGS, centre);
    Array.from({ length: 8 }, () => ladder.next(running));
    expect(ladder.ladder).toBe(CEILING);

    expect(ladder.next({ ...running, revChanged: true })).toBe(BASE);
    // It climbs again from the base, not from the rung before the change.
    expect(ladder.next(running)).toBe(BASE * FACTOR);
  });

  it('climbs on the un-jittered ladder, so low draws do not compound', () => {
    // Two ladders that differ only in their draw climb the same rungs and sleep different delays.
    const lowLadder = new PollLadder(DEFAULT_POLL_SETTINGS, low);
    const highLadder = new PollLadder(DEFAULT_POLL_SETTINGS, high);
    Array.from({ length: 4 }, () => {
      lowLadder.next(running);
      highLadder.next(running);
    });
    expect(lowLadder.ladder).toBe(highLadder.ladder);
    expect(lowLadder.ladder).toBe(1350);
  });

  it('does not restart the climb below the base for a too-small previous delay', () => {
    expect(ladderInterval(state({ previousDelay: 0 }))).toBe(BASE * FACTOR);
    expect(ladderInterval(state({ previousDelay: -1 }))).toBe(BASE * FACTOR);
  });
});

describe('the ceiling cadences', () => {
  it('polls at the ceiling while a dialog is open, whatever the ladder had climbed to', () => {
    expect(ladderInterval(state({ anyDialogOpen: true, anyActive: false, previousDelay: BASE }))).toBe(CEILING);
    // An open dialog outranks an active state and a moved rev.
    expect(ladderInterval(state({ anyDialogOpen: true }))).toBe(CEILING);
    expect(ladderInterval(state({ anyDialogOpen: true, revChanged: true }))).toBe(CEILING);
  });

  it('polls at the ceiling as a heartbeat when nothing is active', () => {
    const heartbeat = state({ anyActive: false });
    expect(ladderInterval(heartbeat)).toBe(CEILING);
    expect(nextDelay(heartbeat, centre)).toBe(CEILING);
  });

  it('never stops: every reachable state yields a finite delay of at least the base', () => {
    const flags = [true, false];
    const previous = [null, BASE, 1350, CEILING, 0, -1];
    const advised = [undefined, null, 0, -5, 400, 60_000];
    flags.forEach((anyActive) =>
      flags.forEach((anyDialogOpen) =>
        flags.forEach((revChanged) =>
          previous.forEach((previousDelay) =>
            advised.forEach((pollAfterMs) => {
              [low, centre, high].forEach((draw) => {
                const delay = nextDelay({ anyActive, anyDialogOpen, revChanged, previousDelay, pollAfterMs }, draw);
                expect(Number.isFinite(delay)).toBe(true);
                expect(delay).toBeGreaterThanOrEqual(BASE);
              });
            }),
          ),
        ),
      ),
    );
  });
});

describe('poll_after_ms only slows the ladder', () => {
  const ladderValues: { name: string; state: PollLadderState; ladder: number }[] = [
    { name: 'base', state: state(), ladder: BASE },
    { name: 'mid-climb', state: state({ previousDelay: 1350 }), ladder: 2025 },
    { name: 'ceiling', state: state({ anyActive: false }), ladder: CEILING },
  ];
  const floors = [0, 1, 200, 399, 400, 401, 3000, 5000, 30_000];

  ladderValues.forEach(({ name, state: base, ladder }) => {
    floors.forEach((pollAfterMs) => {
      it(`emits no sooner than the floor or the base interval: ${name} ladder, server floor ${pollAfterMs}`, () => {
        [low, centre, high].forEach((draw) => {
          const delay = nextDelay({ ...base, pollAfterMs }, draw);
          expect(delay).toBeGreaterThanOrEqual(pollAfterMs);
          expect(delay).toBeGreaterThanOrEqual(BASE);
        });
        // Above the ladder it slows the client; below it, it changes nothing.
        expect(nextDelay({ ...base, pollAfterMs }, centre)).toBe(Math.max(ladder, pollAfterMs, BASE));
      });
    });
  });

  it('is a floor and never a target: a large value is honoured in full', () => {
    expect(nextDelay(state({ pollAfterMs: 30_000 }), centre)).toBe(30_000);
  });

  it('cannot pull the client below its base interval, whatever the number', () => {
    [0, -1, -100_000, Number.NaN, Number.NEGATIVE_INFINITY, Number.POSITIVE_INFINITY].forEach((pollAfterMs) => {
      expect(nextDelay(state({ pollAfterMs }), low)).toBe(BASE);
    });
  });
});

describe('jitter', () => {
  it('is +/- 20 % and is applied', () => {
    expect(DEFAULT_POLL_SETTINGS.jitterFraction).toBe(0.2);
    expect(jitterMultiplier(low)).toBeCloseTo(0.8, 10);
    expect(jitterMultiplier(centre)).toBeCloseTo(1, 10);
    expect(jitterMultiplier(high)).toBeCloseTo(1.2, 10);

    // At the ceiling both halves of the window are visible, because neither floor is in the way.
    const heartbeat = state({ anyActive: false });
    expect(nextDelay(heartbeat, low)).toBe(4000);
    expect(nextDelay(heartbeat, high)).toBe(6000);
  });

  it('gives N clients restored at the same instant distinct next-poll instants', () => {
    const restoredAt = 1_700_000_000_000;
    const draws = Array.from({ length: 50 }, (_, index) => (index + 0.5) / 50);
    const instants = new Set(draws.map((draw) => restoredAt + nextDelay(state({ anyActive: false }), () => draw)));
    expect(instants.size).toBe(50);
  });

  it('draws once per delay, so the same state does not produce the same number twice', () => {
    let counter = 0;
    const draw = (): number => {
      counter += 1;
      return counter / 10;
    };
    const ladder = new PollLadder(DEFAULT_POLL_SETTINGS, draw);
    expect(ladder.next(idle)).not.toBe(ladder.next(idle));
    expect(counter).toBe(2);
  });

  it('follows custom settings', () => {
    const settings = { baseIntervalMs: 100, backoffFactor: 2, ceilingMs: 1000, jitterFraction: 0.5 };
    const ladder = new PollLadder(settings, centre);
    expect(Array.from({ length: 6 }, () => ladder.next(running))).toEqual([100, 200, 400, 800, 1000, 1000]);
    expect(jitterMultiplier(low, settings)).toBeCloseTo(0.5, 10);
    expect(nextDelay(state({ anyActive: false }), low, settings)).toBe(500);
  });
});

describe('PollLadder', () => {
  it('has no rung before the first poll', () => {
    expect(new PollLadder().ladder).toBeNull();
  });

  it('returns to the first-poll state on reset()', () => {
    const ladder = new PollLadder(DEFAULT_POLL_SETTINGS, centre);
    Array.from({ length: 5 }, () => ladder.next(running));
    ladder.reset();
    expect(ladder.ladder).toBeNull();
    expect(ladder.next(running)).toBe(BASE);
    expect(ladder.next(running)).toBe(BASE * FACTOR);
  });

  it('resumes on the base rung, jittered and floored at the base, whatever the state would answer', () => {
    const ladder = new PollLadder(DEFAULT_POLL_SETTINGS, centre);
    Array.from({ length: 8 }, () => ladder.next(idle));
    expect(ladder.ladder).toBe(CEILING);

    expect(ladder.resume()).toBe(BASE);
    expect(ladder.ladder).toBe(BASE);
    // The climb continues from the base rung.
    expect(ladder.next(running)).toBe(BASE * FACTOR);

    expect(new PollLadder(DEFAULT_POLL_SETTINGS, low).resume()).toBe(BASE);
    expect(new PollLadder(DEFAULT_POLL_SETTINGS, high).resume()).toBe(Math.round(BASE * jitterMultiplier(high)));
  });
});
