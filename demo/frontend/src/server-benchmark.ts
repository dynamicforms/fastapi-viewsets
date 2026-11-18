/**
 * The server-side benchmark API: `GET /benchmark` describes the setup, `POST /benchmark/runs` runs one
 * cell (transport, executor, DB API, operation, concurrency) with the load generator and returns its result.
 */

import axios from 'axios';

import type { Transport } from './viewsets';

export type Executor = 'fastapi' | 'classic' | 'hybrid';
export type DbApi = 'django' | 'adorm';
export type Operation = 'retrieve' | 'list';

export const EXECUTORS: Executor[] = ['fastapi', 'classic', 'hybrid'];
export const DB_APIS: DbApi[] = ['django', 'adorm'];
export const TRANSPORTS: Transport[] = ['rest', 'muxws'];
export const LEVELS = [10, 100, 1000] as const;
export const LATENCY_PRESETS = [0, 2, 10, 50] as const;

export const EXECUTOR_LABELS: Record<Executor, string> = {
  fastapi: 'FastAPI',
  classic: 'Celery classic',
  hybrid: 'Celery hybrid',
};
export const DB_API_LABELS: Record<DbApi, string> = { django: 'Django ORM', adorm: 'adorm' };
export const TRANSPORT_LABELS: Record<Transport, string> = { rest: 'REST', muxws: 'muxws' };

/** One executor as `GET /benchmark` describes it. */
export interface ExecutorInfo {
  name: Executor;
  /** Its name for people, e.g. `Celery hybrid`. */
  label: string;
  available: boolean;
  reason: string | null;
  /** Configured processes, e.g. `2 prefork + 2 async processes (aconcurrency 200)`. */
  description: string;
  processes: number;
  prefork_processes: number;
  async_processes: number;
  /** `--aconcurrency` over the async processes; null without them. */
  async_concurrency: number | null;
  /** Processes the running worker reports; 0 when no worker answers. */
  active_processes: number;
  active_prefork_processes: number;
  active_async_processes: number;
}

export interface BenchmarkInfo {
  available: boolean;
  reason: string | null;
  running: boolean;
  /** The benchmark database, e.g. `PostgreSQL at 127.0.0.1:5432, database adorm_fastapi_viewsets_demo`. */
  database: string | null;
  /** The round-trip time the latency proxy adds now; null without the proxy. */
  db_latency_ms: number | null;
  latency_proxy: boolean;
  adorm_available: boolean;
  adorm_reason: string | null;
  adorm_pool: number;
  adorm_driver: string;
  library_size: number;
  /** `fastapi`, `classic` and `hybrid`, in that order. */
  executors: ExecutorInfo[];
}

export interface RunRequest {
  transport: Transport;
  executor: Executor;
  db_api: DbApi;
  operation: Operation;
  concurrency: number;
  duration_s: number;
  db_latency_ms: number;
}

/** Latencies of the completed requests, in ms; null when none completed. */
export interface LatencyStats {
  p50: number | null;
  p95: number | null;
  p99: number | null;
  mean: number | null;
  max: number | null;
}

/** The load generator's output for one cell. */
export interface RunResult {
  completed: number;
  errors: number;
  /** `errors` / (`completed` + `errors`), 0 to 1. */
  error_rate: number;
  /** Error count per kind: `timeout`, an HTTP status or an exception class. */
  error_kinds: Record<string, number>;
  /** Completed requests per second of measured wall time. */
  throughput: number;
  /** Latency of completed requests, in ms. */
  latency_ms: LatencyStats;
  /** CPU time the load generator used, in seconds. */
  loadgen_cpu_s: number;
  /** Length of the measured window, in seconds. */
  measured_s: number;
  started: string;
  finished: string;
}

export async function benchmarkInfo(): Promise<BenchmarkInfo> {
  return (await axios.get<BenchmarkInfo>('/benchmark')).data;
}

export async function runCell(request: RunRequest): Promise<RunResult> {
  return (await axios.post<RunResult>('/benchmark/runs', request)).data;
}

/** Errors as a percentage of all finished requests. */
export function errorRate(result: RunResult): number {
  return 100 * result.error_rate;
}

/** An executor by name; undefined when `GET /benchmark` does not list it. */
export function executorInfo(info: BenchmarkInfo, name: Executor): ExecutorInfo | undefined {
  return info.executors.find((one) => one.name === name);
}

/** "Celery hybrid: 2 prefork + 2 async processes (aconcurrency 200), 4 active". */
export function describeExecutor(info: ExecutorInfo): string {
  return `${info.label}: ${info.description}, ${info.active_processes} active`;
}

/** A latency in ms with one decimal; a dash when none was measured. */
export function formatMs(value: number | null): string {
  return value === null ? '–' : value.toFixed(1);
}
