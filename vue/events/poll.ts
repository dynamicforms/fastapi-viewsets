/**
 * The polling ladder: the cadence of a client that polls for changes, as a pure function.
 *
 * Nothing here touches a timer, a clock, a socket or `axios`. `nextDelay(state, draw)` takes all the
 * ladder may know (is anything active, is a dialog open, did any `rev` change, the previous ladder
 * interval, the server's advice) and returns a number of milliseconds. A scheduler is a `setTimeout`
 * loop around it, so the cadence is testable without advancing a clock.
 *
 * **The server's advice only slows the ladder down.** `pollAfterMs` (`poll_after_ms` on the wire) is
 * a floor: the delay is `max(pollAfterMs, ladder)`, jittered, and then floored again at `pollAfterMs`
 * and at the base interval, so the lower half of the jitter undercuts neither. Without the second
 * floor a `poll_after_ms` of `0`, or any other too-small value, makes every client polling that
 * server poll it as often as the jitter allows. A client may poll later than advised and never
 * sooner.
 *
 * **`previousDelay` is the previous ladder interval, not the previous emitted delay.** Feeding the
 * jittered delay back in compounds the jitter: a run of low draws climbs the ladder 20 % faster than
 * ×1.5, and a run of high ones holds it below the ceiling. The ladder is deterministic and the jitter
 * is applied once, to the number the caller sleeps for. `PollLadder` carries the un-jittered value.
 */

/** The four constants of the cadence. */
export interface PollSettings {
  /** The interval while anything is active, and after any `rev` change. */
  baseIntervalMs: number;
  /** The multiplier applied when no `rev` changed. */
  backoffFactor: number;
  /** The upper bound of the climb, and the cadence while a dialog is open and while idle. */
  ceilingMs: number;
  /** The jitter, as a fraction either way, so tabs restored at the same instant do not synchronise. */
  jitterFraction: number;
}

/** 400 ms base, ×1.5 backoff, a 5 s ceiling and ±20 % jitter. */
export const DEFAULT_POLL_SETTINGS: PollSettings = {
  baseIntervalMs: 400,
  backoffFactor: 1.5,
  ceilingMs: 5000,
  jitterFraction: 0.2,
};

/**
 * All the ladder may know. It carries no wire type, so one ladder serves every kind of poll and
 * cannot tell which one it is answering.
 */
export interface PollLadderState {
  /**
   * Anything polled is active and expected to change. `false` polls at the ceiling. A caller that
   * cannot tell passes `true`: it is waiting for something it expects, and the ceiling would delay it.
   */
  anyActive: boolean;
  /** A dialog is open on anything polled; polls at the ceiling. */
  anyDialogOpen: boolean;
  /** Any polled `rev` moved since the previous read. */
  revChanged: boolean;
  /** The previous **un-jittered** ladder interval, or `null` on the first poll. */
  previousDelay: number | null;
  /** The server's `poll_after_ms`. Only ever raises the delay. */
  pollAfterMs?: number | null;
}

/** A uniform draw in `[0, 1)`. Injected so the jitter is assertable; defaults to `Math.random`. */
export type Draw = () => number;

/**
 * The ladder interval, with no jitter and no server advice.
 *
 * The branches apply in order. An open dialog answers the ceiling before anything else. Idle answers
 * the ceiling. No branch answers "stop": a client that stopped polling while nothing was active would
 * not learn that something started.
 *
 * Whether the scheduler sleeps this interval at all is not the ladder's decision. A healthy push
 * transport suspends the loop, and losing it resumes the loop on the base rung (`PollLadder.reset()`
 * and `PollLadder.resume()`); the state has no way to express "stopped".
 */
export function ladderInterval(state: PollLadderState, settings: PollSettings = DEFAULT_POLL_SETTINGS): number {
  const { baseIntervalMs, backoffFactor, ceilingMs } = settings;
  if (state.anyDialogOpen) return ceilingMs;
  if (!state.anyActive) return ceilingMs;
  if (state.revChanged) return baseIntervalMs;
  if (state.previousDelay === null || state.previousDelay === undefined) return baseIntervalMs;
  // `Math.max(previous, base)` keeps a too-small `previousDelay`, such as a jittered delay, from
  // restarting the climb below the base interval.
  return Math.min(Math.max(state.previousDelay, baseIntervalMs) * backoffFactor, ceilingMs);
}

/** The jitter multiplier, in `[1 - jitterFraction, 1 + jitterFraction)`. */
export function jitterMultiplier(draw: Draw = Math.random, settings: PollSettings = DEFAULT_POLL_SETTINGS): number {
  return 1 + (draw() * 2 - 1) * settings.jitterFraction;
}

/**
 * Milliseconds until the next poll; the one function a scheduler calls.
 *
 * Raises the ladder interval to the server's floor, jitters it, and floors the result again at both
 * the server's floor and the base interval. The second floor is the one that holds against a
 * `pollAfterMs` of `0`, a negative or non-finite one, and the low end of the jitter.
 */
export function nextDelay(
  state: PollLadderState,
  draw: Draw = Math.random,
  settings: PollSettings = DEFAULT_POLL_SETTINGS,
): number {
  const advised = typeof state.pollAfterMs === 'number' && Number.isFinite(state.pollAfterMs) ? state.pollAfterMs : 0;
  const floor = Math.max(advised, settings.baseIntervalMs);
  const jittered = Math.max(ladderInterval(state, settings), advised) * jitterMultiplier(draw, settings);
  // Rounded because no timer honours a fractional millisecond; rounding a value at or above an
  // integer floor cannot drop it below that floor.
  return Math.max(Math.round(jittered), floor);
}

/**
 * The state a scheduler keeps between polls: the previous **ladder** interval.
 *
 * Held here rather than by the caller, because the caller sees only the jittered delay, and feeding
 * that back in as `previousDelay` compounds the jitter.
 */
export class PollLadder {
  private previous: number | null = null;

  private readonly settings: PollSettings;

  private readonly draw: Draw;

  constructor(settings: PollSettings = DEFAULT_POLL_SETTINGS, draw: Draw = Math.random) {
    this.settings = settings;
    this.draw = draw;
  }

  /** The un-jittered interval the last `next()` climbed to, or `null` before the first one. */
  get ladder(): number | null {
    return this.previous;
  }

  /** Climbs one rung and returns the jittered milliseconds to wait. The rung is remembered. */
  next(state: Omit<PollLadderState, 'previousDelay'>): number {
    const full: PollLadderState = { ...state, previousDelay: this.previous };
    this.previous = ladderInterval(full, this.settings);
    return nextDelay(full, this.draw, this.settings);
  }

  /** Returns the ladder to its state before the first poll; the next `next()` answers the base rung. */
  reset(): void {
    this.previous = null;
  }

  /**
   * Returns the base interval, jittered and floored at the base, and sets the climb to start from it:
   * what a loop schedules when it resumes after a suspension, such as a lost push transport.
   *
   * It does not consult `ladderInterval`. An open dialog and an idle state both answer the ceiling
   * there, and both are routinely true of a client that has just lost its socket; the first poll after
   * the loss would then come a ceiling later rather than one base interval later.
   */
  resume(): number {
    const { baseIntervalMs } = this.settings;
    this.previous = baseIntervalMs;
    return Math.max(Math.round(baseIntervalMs * jitterMultiplier(this.draw, this.settings)), baseIntervalMs);
  }
}
