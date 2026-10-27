/**
 * The polling ladder: the delay between polls as a pure function of the ladder state, free of timers.
 * The server's `poll_after_ms` is only a floor: a client never polls sooner than advised, nor sooner
 * than the base interval. `previousDelay` is the un-jittered ladder interval; feeding back the jittered
 * delay would compound the jitter.
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

/** All the ladder may know; independent of the wire type, so it serves every kind of poll. */
export interface PollLadderState {
  /** Anything polled is expected to change; `false` polls at the ceiling. Pass `true` when unsure. */
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
 * The ladder interval, without jitter or server advice: the ceiling for an open dialog or idle state,
 * the base after a `rev` change or on the first poll, else the previous interval times the backoff.
 */
export function ladderInterval(state: PollLadderState, settings: PollSettings = DEFAULT_POLL_SETTINGS): number {
  const { baseIntervalMs, backoffFactor, ceilingMs } = settings;
  if (state.anyDialogOpen) return ceilingMs;
  if (!state.anyActive) return ceilingMs;
  if (state.revChanged) return baseIntervalMs;
  if (state.previousDelay === null || state.previousDelay === undefined) return baseIntervalMs;
  // keeps a too-small `previousDelay` from restarting the climb below the base interval
  return Math.min(Math.max(state.previousDelay, baseIntervalMs) * backoffFactor, ceilingMs);
}

/** The jitter multiplier, in `[1 - jitterFraction, 1 + jitterFraction)`. */
export function jitterMultiplier(draw: Draw = Math.random, settings: PollSettings = DEFAULT_POLL_SETTINGS): number {
  return 1 + (draw() * 2 - 1) * settings.jitterFraction;
}

/**
 * Milliseconds until the next poll: the ladder interval raised to `pollAfterMs`, jittered, then floored
 * again at `pollAfterMs` and the base interval (which also guards against a `0` or invalid advice).
 */
export function nextDelay(
  state: PollLadderState,
  draw: Draw = Math.random,
  settings: PollSettings = DEFAULT_POLL_SETTINGS,
): number {
  const advised = typeof state.pollAfterMs === 'number' && Number.isFinite(state.pollAfterMs) ? state.pollAfterMs : 0;
  const floor = Math.max(advised, settings.baseIntervalMs);
  const jittered = Math.max(ladderInterval(state, settings), advised) * jitterMultiplier(draw, settings);
  return Math.max(Math.round(jittered), floor);
}

/** The state a scheduler keeps between polls: the previous un-jittered ladder interval. */
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
   * The jittered base interval (floored at the base), restarting the climb from it: the first delay
   * after a suspension such as a lost push transport. Ignores dialog and idle state, which would
   * otherwise delay that first poll to the ceiling.
   */
  resume(): number {
    const { baseIntervalMs } = this.settings;
    this.previous = baseIntervalMs;
    return Math.max(Math.round(baseIntervalMs * jitterMultiplier(this.draw, this.settings)), baseIntervalMs);
  }
}
