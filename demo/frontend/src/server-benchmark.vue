<template>
  <div class="server-benchmark">
    <v-card variant="tonal" class="mb-4" data-testid="benchmark-setup">
      <v-card-title class="text-subtitle-1">Setup</v-card-title>
      <v-card-text>
        <div v-if="!info">{{ infoError ?? 'Reading the benchmark setup…' }}</div>
        <template v-else>
          <div v-if="!info.available" class="text-warning mb-2" data-testid="benchmark-unavailable">
            Benchmark unavailable: {{ info.reason ?? 'no reason given' }}
          </div>
          <ul class="setup">
            <li
              v-for="executor in info.executors"
              :key="executor.name"
              :data-testid="`executor-${executor.name}`"
              :data-available="executor.available"
              :data-active="executor.active_processes"
            >
              <v-icon
                size="small"
                :color="executor.available ? 'success' : 'error'"
                :icon="executor.available ? 'mdi-check-circle' : 'mdi-close-circle'"
              />
              {{ describeExecutor(executor) }}
              <span v-if="!executor.available" class="text-medium-emphasis">
                — unavailable: {{ executor.reason ?? 'no reason given' }}
              </span>
            </li>
            <li data-testid="benchmark-loadgen-processes" :data-processes="info.loadgen_processes">
              {{ describeLoadgen(info) }}
            </li>
            <li>adorm pool: {{ info.adorm_pool }} connections per process</li>
            <li v-if="!info.adorm_available" class="text-medium-emphasis" data-testid="adorm-unavailable">
              adorm: unavailable — {{ info.adorm_reason ?? 'no reason given' }}
            </li>
            <li>Database: {{ database }}</li>
            <li>
              Current injected DB latency:
              {{ info.db_latency_ms === null ? 'none (no latency proxy)' : `${info.db_latency_ms} ms round trip` }}
            </li>
          </ul>
        </template>
      </v-card-text>
    </v-card>

    <v-card variant="outlined" class="mb-4">
      <v-card-text>
        <div class="controls">
          <fieldset>
            <legend>Transport</legend>
            <v-checkbox
              v-for="name in TRANSPORTS"
              :key="name"
              v-model="transports"
              :value="name"
              :label="TRANSPORT_LABELS[name]"
              :data-testid="`bench-transport-${name}`"
              density="compact"
              hide-details
            />
          </fieldset>
          <fieldset>
            <legend>Executor</legend>
            <v-checkbox
              v-for="name in EXECUTORS"
              :key="name"
              v-model="executors"
              :value="name"
              :label="EXECUTOR_LABELS[name]"
              :disabled="!executorAvailable(name)"
              :data-testid="`bench-executor-${name}`"
              density="compact"
              hide-details
            />
          </fieldset>
          <fieldset>
            <legend>DB API</legend>
            <v-checkbox
              v-for="name in DB_APIS"
              :key="name"
              v-model="dbApis"
              :value="name"
              :label="DB_API_LABELS[name]"
              :disabled="!dbApiAvailable(name)"
              :data-testid="`bench-db-${name}`"
              density="compact"
              hide-details
            />
          </fieldset>
          <fieldset>
            <legend>Concurrency</legend>
            <v-checkbox
              v-for="level in LEVELS"
              :key="level"
              v-model="levels"
              :value="level"
              :label="String(level)"
              :data-testid="`bench-level-${level}`"
              density="compact"
              hide-details
            />
          </fieldset>
          <fieldset class="settings">
            <legend>Run</legend>
            <label class="operation">
              <span class="text-caption">Operation</span>
              <select v-model="operation" data-testid="bench-operation">
                <option v-for="one in OPERATIONS" :key="one.value" :value="one.value">{{ one.title }}</option>
              </select>
            </label>
            <v-text-field
              v-model.number="duration"
              type="number"
              :min="1"
              :max="30"
              label="Duration"
              suffix="s"
              data-testid="bench-duration"
              density="compact"
              hide-details
            />
            <v-text-field
              v-model.number="latency"
              type="number"
              :min="0"
              :max="100"
              label="DB latency (round trip)"
              suffix="ms"
              :disabled="!info?.latency_proxy"
              data-testid="bench-latency"
              density="compact"
              hide-details
            />
            <div class="d-flex ga-1">
              <v-btn
                v-for="preset in LATENCY_PRESETS"
                :key="preset"
                size="x-small"
                :variant="latency === preset ? 'flat' : 'tonal'"
                :aria-pressed="latency === preset"
                :disabled="!info?.latency_proxy"
                :data-testid="`bench-latency-preset-${preset}`"
                @click="latency = preset"
              >
                {{ preset }} ms
              </v-btn>
            </div>
          </fieldset>
        </div>
        <div class="d-flex align-center flex-wrap ga-2 mt-3">
          <v-btn color="primary" :loading="running" :disabled="!canRun" data-testid="bench-run" @click="run">
            Run {{ plan.length }} {{ plan.length === 1 ? 'cell' : 'cells' }}
          </v-btn>
          <v-btn :disabled="!running || stopRequested" data-testid="bench-stop" @click="stopRequested = true">
            Stop
          </v-btn>
          <v-btn variant="text" :disabled="running" data-testid="bench-clear" @click="clear">Clear</v-btn>
          <span class="text-caption" data-testid="bench-progress" :data-running="running">{{ progress }}</span>
        </div>
      </v-card-text>
    </v-card>

    <v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-4" data-testid="bench-error">
      {{ error }}
    </v-alert>

    <div class="results-scroll mb-4">
      <table class="results" data-testid="bench-results">
        <thead>
          <tr>
            <th rowspan="2" scope="col" class="row-head">Executor · DB API · transport</th>
            <th v-for="level in shownLevels" :key="level" colspan="5" scope="colgroup" class="group">
              c = {{ level }}
            </th>
          </tr>
          <tr>
            <template v-for="level in shownLevels" :key="level">
              <th scope="col" class="group">req/s</th>
              <th scope="col">p50 ms</th>
              <th scope="col">p95 ms</th>
              <th scope="col">p99 ms</th>
              <th scope="col">errors</th>
            </template>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!rows.length">
            <td :colspan="1 + 5 * shownLevels.length" class="text-medium-emphasis">No results yet.</td>
          </tr>
          <tr v-for="row in rows" :key="row.key">
            <th scope="row" class="row-head">{{ rowLabel(row) }}</th>
            <template v-for="level in shownLevels" :key="level">
              <template v-if="results[cellKey(row, level)]">
                <td
                  class="group"
                  :data-testid="`bench-cell-${cellKey(row, level)}`"
                  :data-throughput="results[cellKey(row, level)].throughput"
                  :data-p50="results[cellKey(row, level)].latency_ms.p50"
                  :data-p95="results[cellKey(row, level)].latency_ms.p95"
                  :data-p99="results[cellKey(row, level)].latency_ms.p99"
                  :data-error-rate="errorRate(results[cellKey(row, level)])"
                  :title="cellTitle(results[cellKey(row, level)])"
                >
                  {{ results[cellKey(row, level)].throughput.toFixed(0) }}
                </td>
                <td>{{ formatMs(results[cellKey(row, level)].latency_ms.p50) }}</td>
                <td>{{ formatMs(results[cellKey(row, level)].latency_ms.p95) }}</td>
                <td>{{ formatMs(results[cellKey(row, level)].latency_ms.p99) }}</td>
                <td :class="{ 'text-error': results[cellKey(row, level)].errors > 0 }">
                  {{ errorRate(results[cellKey(row, level)]).toFixed(1) }} %
                </td>
              </template>
              <template v-else>
                <td class="group text-medium-emphasis">–</td>
                <td colspan="4" />
              </template>
            </template>
          </tr>
        </tbody>
      </table>
      <div v-if="loadgenSummary" class="text-caption mt-1" data-testid="bench-loadgen">{{ loadgenSummary }}</div>
    </div>

    <div class="charts mb-4" data-testid="bench-chart">
      <figure v-for="level in chartLevels" :key="level" class="chart" :data-testid="`bench-chart-${level}`">
        <figcaption class="text-subtitle-2">Throughput at c = {{ level }} (req/s)</figcaption>
        <div v-for="row in rows" :key="row.key" class="bar-row">
          <span class="bar-label">{{ rowLabel(row) }}</span>
          <span class="bar-track">
            <span
              v-if="results[cellKey(row, level)]"
              class="bar"
              :class="`bar-${row.dbApi}`"
              :style="{ width: `${barWidth(results[cellKey(row, level)].throughput)}%` }"
            />
          </span>
          <span class="bar-value">{{ results[cellKey(row, level)]?.throughput.toFixed(0) ?? '–' }}</span>
        </div>
      </figure>
    </div>

    <v-card variant="text" class="explanation text-body-2">
      <v-card-title class="text-subtitle-1 px-0">What is measured</v-card-title>
      <p>
        Each cell is one run of a load generator against the benchmark processes: <em>c</em> virtual users each send the
        next request as soon as the previous one answers, for the chosen duration after a one-second warm-up. The table
        shows completed requests per second, latency percentiles of the completed requests and the share of failed ones.
        Every cell reads the same PostgreSQL table; only the executor, the DB API and the transport differ. A request
        that waits longer than the 10 s client timeout counts as failed: an overloaded cell can show a throughput
        together with a 100 % error rate, when every answer in the window belongs to a request started before it.
      </p>
      <p>
        The benchmark viewsets run in their own uvicorn processes, as many as the setup lists, apart from the process
        that serves this page. Every benchmark request, also one Celery executes, passes through one of them; the load
        generator's processes are assigned to them in turn.
      </p>
      <p>
        A proxy between the server and PostgreSQL delays every answer by the chosen DB latency. It stands in for a
        remote database or slow queries, where a process spends most of a request waiting for the database.
      </p>
      <p>
        <strong>Django ORM</strong> stays flat as concurrency grows: outside Django's ASGI handler, its async methods
        and <code>sync_to_async(thread_sensitive=True)</code> run every query of a process on one thread, so each
        process has one query in flight at a time. <strong>adorm</strong> awaits its queries on a connection pool, so
        one process has as many queries in flight as the pool has connections.
      </p>
      <p>
        <strong>Celery classic</strong> runs one task per prefork process, so its throughput is capped at its process
        count divided by the request time, on both DB APIs. The <strong>Celery hybrid</strong> worker adds async
        processes that share up to <code>--aconcurrency</code> tasks at a time on their event loops; there adorm's
        queries overlap, while Django ORM's still run one at a time per process.
      </p>
      <p>
        The load generator runs in several processes on the same machine, as many as the setup lists, splits the virtual
        users as evenly as possible over them and competes with the server for CPU. The CPU time of its busiest process
        is shown below the table, and each cell's tooltip shows that and the total of all its processes; a process near
        one CPU second per second is saturated and caps every row.
      </p>
    </v-card>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';

import { errorBodyOf, toErrorDescription } from '../../../vue/errors';

import {
  benchmarkInfo,
  DB_API_LABELS,
  DB_APIS,
  describeExecutor,
  describeLoadgen,
  errorRate,
  EXECUTOR_LABELS,
  executorInfo,
  EXECUTORS,
  formatMs,
  LATENCY_PRESETS,
  LEVELS,
  runCell,
  TRANSPORT_LABELS,
  TRANSPORTS,
  type BenchmarkInfo,
  type DbApi,
  type Executor,
  type Operation,
  type RunResult,
} from './server-benchmark';
import { demoBackends, type Transport } from './viewsets';

const props = defineProps<{ active: boolean }>();

const OPERATIONS: { title: string; value: Operation }[] = [
  { title: 'retrieve (one record by id)', value: 'retrieve' },
  { title: 'list (first page of 50)', value: 'list' },
];

interface Row {
  key: string;
  executor: Executor;
  dbApi: DbApi;
  transport: Transport;
}

interface Cell extends Row {
  level: number;
}

const info = ref<BenchmarkInfo | null>(null);
const infoError = ref<string | null>(null);
const backendLabels = ref<string[]>([]);

const transports = ref<Transport[]>(['rest']);
const executors = ref<Executor[]>([]);
const dbApis = ref<DbApi[]>([...DB_APIS]);
const levels = ref<number[]>([10, 100]);
const operation = ref<Operation>('retrieve');
const duration = ref(5);
const latency = ref(10);

const running = ref(false);
const stopRequested = ref(false);
const progress = ref('');
const error = ref<string | null>(null);

/** Results by `cellKey`; rows in the order they were first planned. */
const results = ref<Record<string, RunResult>>({});
const rows = ref<Row[]>([]);
const resultLevels = ref<number[]>([]);

function failureText(what: string, e: unknown): string {
  const description = toErrorDescription(errorBodyOf(e));
  return `${what}: ${description.detail} (${description.code})`;
}

function executorAvailable(name: Executor): boolean {
  return !!info.value?.available && !!executorInfo(info.value, name)?.available;
}

function dbApiAvailable(name: DbApi): boolean {
  return name !== 'adorm' || !!info.value?.adorm_available;
}

const database = computed(() => info.value?.database ?? (backendLabels.value.join(', ') || 'unknown'));

async function refresh() {
  try {
    const fresh = await benchmarkInfo();
    const first = info.value === null;
    info.value = fresh;
    infoError.value = null;
    if (first) {
      executors.value = EXECUTORS.filter(executorAvailable);
      dbApis.value = DB_APIS.filter(dbApiAvailable);
      latency.value = fresh.db_latency_ms ?? 0;
    }
  } catch (e) {
    infoError.value = failureText('GET /benchmark failed', e);
  }
}

void demoBackends().then(
  (backends) => {
    backendLabels.value = backends.filter((one) => one.available && one.name !== 'memory').map((one) => one.label);
  },
  () => undefined,
);

watch(
  () => props.active,
  (active) => {
    if (active && !running.value) void refresh();
  },
  { immediate: true },
);

function rowKey(executor: Executor, dbApi: DbApi, transport: Transport): string {
  return `${executor}-${dbApi}-${transport}`;
}

function cellKey(row: Row, level: number): string {
  return `${row.key}-${level}`;
}

function rowLabel(row: Row): string {
  return `${EXECUTOR_LABELS[row.executor]} · ${DB_API_LABELS[row.dbApi]} · ${TRANSPORT_LABELS[row.transport]}`;
}

/** The selected cells, a concurrency level at a time so every level's chart fills in turn. */
const plan = computed<Cell[]>(() => {
  const cells: Cell[] = [];
  for (const level of LEVELS.filter((one) => levels.value.includes(one))) {
    for (const executor of EXECUTORS.filter((one) => executors.value.includes(one) && executorAvailable(one))) {
      for (const dbApi of DB_APIS.filter((one) => dbApis.value.includes(one) && dbApiAvailable(one))) {
        for (const transport of TRANSPORTS.filter((one) => transports.value.includes(one))) {
          cells.push({ key: rowKey(executor, dbApi, transport), executor, dbApi, transport, level });
        }
      }
    }
  }
  return cells;
});

const canRun = computed(() => !running.value && !!info.value?.available && plan.value.length > 0);

const shownLevels = computed(() => {
  const planned = running.value ? plan.value.map((cell) => cell.level) : [];
  return LEVELS.filter((level) => resultLevels.value.includes(level) || planned.includes(level));
});

const chartLevels = computed(() => LEVELS.filter((level) => resultLevels.value.includes(level)));

const maxThroughput = computed(() => Math.max(1, ...Object.values(results.value).map((one) => one.throughput)));

/** Bars share one scale across levels, so the growth with concurrency is visible. */
function barWidth(throughput: number): number {
  return (100 * throughput) / maxThroughput.value;
}

function cellTitle(result: RunResult): string {
  const kinds = Object.entries(result.error_kinds)
    .map(([kind, count]) => `${kind}: ${count}`)
    .join(', ');
  return [
    `${result.completed} completed, ${result.errors} errors${kinds ? ` (${kinds})` : ''}`,
    `latency mean ${formatMs(result.latency_ms.mean)} ms, max ${formatMs(result.latency_ms.max)} ms`,
    `load generator CPU ${result.loadgen_cpu_s.toFixed(2)} s in ${result.loadgen_processes} processes, ` +
      `busiest process ${result.loadgen_cpu_max_s.toFixed(2)} s`,
  ].join('\n');
}

/** The run whose busiest load generator process used the most CPU, relative to the run's active wall time. */
const loadgenSummary = computed(() => {
  let busiest: { share: number; cpu: number; wall: number; processes: number } | null = null;
  for (const result of Object.values(results.value)) {
    const wall = result.loadgen_active_s;
    if (!(wall > 0)) continue;
    const share = result.loadgen_cpu_max_s / wall;
    if (!busiest || share > busiest.share) {
      busiest = { share, cpu: result.loadgen_cpu_max_s, wall, processes: result.loadgen_processes };
    }
  }
  if (!busiest) return '';
  return (
    `Load generator, busiest process of ${busiest.processes}: ${busiest.cpu.toFixed(2)} s CPU in ` +
    `${busiest.wall.toFixed(1)} s (${(100 * busiest.share).toFixed(0)} % of one CPU).`
  );
});

function clamp(value: number, min: number, max: number, fallback: number): number {
  return Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;
}

async function run() {
  const cells = plan.value;
  if (!cells.length) return;
  running.value = true;
  stopRequested.value = false;
  error.value = null;
  duration.value = clamp(duration.value, 1, 30, 5);
  latency.value = info.value?.latency_proxy ? clamp(latency.value, 0, 100, 10) : 0;
  for (const cell of cells) {
    if (!rows.value.some((row) => row.key === cell.key)) {
      rows.value = [
        ...rows.value,
        { key: cell.key, executor: cell.executor, dbApi: cell.dbApi, transport: cell.transport },
      ];
    }
  }
  try {
    for (const [index, cell] of cells.entries()) {
      if (stopRequested.value) {
        progress.value = `Stopped after ${index} of ${cells.length} cells.`;
        return;
      }
      progress.value =
        `cell ${index + 1} of ${cells.length}: ${EXECUTOR_LABELS[cell.executor]} · ` +
        `${DB_API_LABELS[cell.dbApi]} · ${TRANSPORT_LABELS[cell.transport]} · c=${cell.level}`;
      let result: RunResult;
      try {
        result = await runCell({
          transport: cell.transport,
          executor: cell.executor,
          db_api: cell.dbApi,
          operation: operation.value,
          concurrency: cell.level,
          duration_s: duration.value,
          db_latency_ms: latency.value,
        });
      } catch (e) {
        error.value = failureText(`Cell ${index + 1} failed`, e);
        progress.value = `Failed at cell ${index + 1} of ${cells.length}.`;
        return;
      }
      results.value = { ...results.value, [cellKey(cell, cell.level)]: result };
      if (!resultLevels.value.includes(cell.level)) resultLevels.value = [...resultLevels.value, cell.level];
    }
    progress.value = `Done: ${cells.length} ${cells.length === 1 ? 'cell' : 'cells'}.`;
  } finally {
    running.value = false;
    stopRequested.value = false;
    void refresh();
  }
}

function clear() {
  results.value = {};
  rows.value = [];
  resultLevels.value = [];
  progress.value = '';
  error.value = null;
}
</script>

<style scoped>
.setup {
  list-style: none;
  padding: 0;
}
.setup li {
  padding: 0.1em 0;
}
.controls {
  display: flex;
  flex-wrap: wrap;
  gap: 1em 2em;
}
.controls fieldset {
  border: none;
  min-width: 8em;
}
.controls legend {
  font-size: 0.8rem;
  opacity: 0.7;
}
.controls .settings {
  display: flex;
  flex-direction: column;
  gap: 0.5em;
  min-width: 14em;
}
.operation {
  display: flex;
  flex-direction: column;
}
.operation select {
  padding: 0.3em 0.4em;
  border: 1px solid rgba(128, 128, 128, 0.5);
  border-radius: 4px;
  color: inherit;
  background-color: transparent;
}
.results-scroll {
  overflow-x: auto;
}
.results {
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
  font-size: 0.85rem;
}
.results th,
.results td {
  padding: 0.2em 0.6em;
  text-align: right;
  white-space: nowrap;
}
.results .row-head {
  text-align: left;
}
.results tbody .row-head {
  font-weight: normal;
}
.results .group {
  border-left: 1px solid rgba(128, 128, 128, 0.5);
}
.results tbody tr:nth-child(even) {
  background-color: rgba(128, 128, 128, 0.1);
}
.charts {
  display: flex;
  flex-wrap: wrap;
  gap: 1.5em;
}
.chart {
  flex: 1 1 22em;
  max-width: 40em;
}
.bar-row {
  display: grid;
  grid-template-columns: minmax(10em, 16em) 1fr 4em;
  align-items: center;
  gap: 0.5em;
  font-size: 0.8rem;
}
.bar-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bar-track {
  height: 0.9em;
  background-color: rgba(128, 128, 128, 0.15);
  border-radius: 2px;
}
.bar {
  display: block;
  height: 100%;
  border-radius: 2px;
}
.bar-django {
  background-color: rgb(var(--v-theme-secondary));
}
.bar-adorm {
  background-color: rgb(var(--v-theme-primary));
}
.bar-value {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.explanation p {
  margin-bottom: 0.6em;
  max-width: 60em;
}
</style>
