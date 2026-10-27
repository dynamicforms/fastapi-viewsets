/**
 * Latency comparison between the REST and muxws transports. **Sequential** measures the per-call
 * cost, one request at a time; **burst** sends N at once and times the last, where the browser's
 * ~6 HTTP/1.1 connections per host queue REST while muxws multiplexes one socket. A discarded
 * warm-up round precedes each measurement.
 */

import type { Backend, Transport } from './viewsets';
import { viewSetFor } from './viewsets';

export interface Stats {
  samples: number[];
  min: number;
  p50: number;
  p95: number;
  max: number;
  mean: number;
}

export interface BenchmarkResult {
  transport: Transport;
  sequential: Stats;
  burstTotalMs: number;
  burstCount: number;
}

function stats(samples: number[]): Stats {
  const sorted = [...samples].sort((a, b) => a - b);
  const at = (q: number) => sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
  return {
    samples,
    min: sorted[0] ?? 0,
    p50: at(0.5) ?? 0,
    p95: at(0.95) ?? 0,
    max: sorted[sorted.length - 1] ?? 0,
    mean: samples.reduce((a, b) => a + b, 0) / (samples.length || 1),
  };
}

/** Ids are picked deterministically so both transports fetch exactly the same rows. */
function idFor(index: number, librarySize: number): number {
  return ((index * 37) % librarySize) + 1;
}

export async function runBenchmark(
  transport: Transport,
  options: { rounds?: number; burst?: number; librarySize?: number; backend?: Backend } = {},
): Promise<BenchmarkResult> {
  const rounds = options.rounds ?? 50;
  const burst = options.burst ?? 50;
  const librarySize = options.librarySize ?? 5000;
  const viewSet = viewSetFor(transport, options.backend ?? 'memory');

  // Warm-up: the first call opens the connection, and that cost is not per-request.
  await Promise.all([viewSet.retrieve(1), viewSet.retrieve(2), viewSet.retrieve(3)]);

  const sequential: number[] = [];
  for (let index = 0; index < rounds; index += 1) {
    const started = performance.now();
    await viewSet.retrieve(idFor(index, librarySize));
    sequential.push(performance.now() - started);
  }

  const burstStarted = performance.now();
  await Promise.all(
    Array.from({ length: burst }, (_unused, index) => viewSet.retrieve(idFor(index + rounds, librarySize))),
  );
  const burstTotalMs = performance.now() - burstStarted;

  return { transport, sequential: stats(sequential), burstTotalMs, burstCount: burst };
}
