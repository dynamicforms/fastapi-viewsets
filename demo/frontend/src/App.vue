<template>
  <v-app>
    <v-main>
      <v-container fluid class="pa-4">
        <div class="d-flex align-center flex-wrap ga-4 mb-4">
          <h1 class="text-h5 mb-0">Music Library</h1>
          <v-btn-toggle v-model="transport" mandatory density="comfortable" color="primary">
            <v-btn value="rest">REST</v-btn>
            <v-btn value="muxws">muxws</v-btn>
          </v-btn-toggle>
          <v-btn-toggle v-model="backend" mandatory density="comfortable" color="secondary">
            <v-btn value="memory">in-memory</v-btn>
            <v-btn value="db">SQLite</v-btn>
          </v-btn-toggle>
          <v-spacer />
          <v-btn :loading="benchmarking" variant="tonal" @click="runComparison">Compare transports</v-btn>
        </div>

        <v-alert v-if="results.length" type="info" variant="tonal" density="compact" class="mb-4">
          <table class="benchmark">
            <thead>
              <tr>
                <th>Transport</th>
                <th colspan="4">Sequential retrieve (ms)</th>
                <th>{{ results[0].burstCount }} at once</th>
              </tr>
              <tr>
                <th />
                <th>min</th>
                <th>p50</th>
                <th>p95</th>
                <th>max</th>
                <th>total (ms)</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="result in results" :key="result.transport">
                <td class="font-weight-medium">{{ result.transport }}</td>
                <td>{{ result.sequential.min.toFixed(2) }}</td>
                <td>{{ result.sequential.p50.toFixed(2) }}</td>
                <td>{{ result.sequential.p95.toFixed(2) }}</td>
                <td>{{ result.sequential.max.toFixed(2) }}</td>
                <td>{{ result.burstTotalMs.toFixed(1) }}</td>
              </tr>
            </tbody>
          </table>
          <div class="text-caption mt-2">
            Sequential cost is close on both — once a connection is warm, HTTP header parsing
            against muxws frame decoding is a small difference. The burst column is the real one:
            a browser opens about six concurrent HTTP/1.1 connections per host, so the seventh
            request waits. muxws multiplexes all of them onto one socket.
          </div>
        </v-alert>

        <div v-if="error" class="text-error mb-4">{{ error }}</div>

        <div class="d-flex align-center ga-3 mb-3">
          <v-btn size="small" :disabled="!page?.hasPrevious || loading" @click="goTo(offset - pageSize)">
            Previous
          </v-btn>
          <v-btn size="small" :disabled="!page?.hasMore || loading" @click="goTo(offset + pageSize)">Next</v-btn>
          <span class="text-caption">
            {{ offset + 1 }}–{{ offset + (page?.results.length ?? 0) }}
            <template v-if="page?.count !== null && page?.count !== undefined">of {{ page.count }}</template>
            · loaded in {{ lastLoadMs.toFixed(1) }} ms over {{ transport }} from {{ backend }}
          </span>
        </div>

        <div v-if="loading && !records.length" class="text-center pa-8">Loading...</div>
        <df-grid
          v-else
          v-model:active-columns="activeColumnDef"
          :columns="columnsResponsive"
          :records="records"
          key-field="id"
          :show-filter-row="true"
          style="height: 70vh"
          @click="(data: any) => console.log('click:', data)"
          @sort="(data: any) => console.log('sort:', data)"
        />
      </v-container>
    </v-main>
  </v-app>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import { createColumn, filterColumns, type ResponsiveColumnDefinitions } from '@dynamicforms/vue-grid';
import type { PaginatedList } from '../../../vue/mixins';
import { runBenchmark, type BenchmarkResult } from './benchmark';
import { viewSetFor, type Backend, type MusicTrack, type Transport } from './viewsets';

const transport = ref<Transport>('rest');
const backend = ref<Backend>('memory');
const records = ref<MusicTrack[]>([]);
const page = ref<PaginatedList<MusicTrack> | null>(null);
const offset = ref(0);
const pageSize = 50;
const loading = ref(true);
const lastLoadMs = ref(0);
const error = ref<string | null>(null);
const activeColumnDef = ref('three-row');

const benchmarking = ref(false);
const results = ref<BenchmarkResult[]>([]);

const columns = [
  createColumn('id', 'Id', 'int', { cssClass: 'text-right' }),
  createColumn('title', 'Title', 'plain'),
  createColumn('artist', 'Artist', 'plain'),
  createColumn('year', 'Year', 'int', { cssClass: 'text-right' }),
  createColumn('duration', 'Duration', 'plain', { cssClass: 'text-right' }),
  createColumn('genres', 'Genres', 'plain'),
  createColumn('rating', 'Rating', 'int', { cssClass: 'text-right' }),
  createColumn('favorite', 'Favorite', 'checkbox'),
  createColumn('play_count', 'Play count', 'int', { cssClass: 'text-right' }),
  createColumn('moods', 'Moods', 'plain'),
  createColumn('language', 'Language', 'plain'),
];

const columnsResponsive: ResponsiveColumnDefinitions = [
  { cssClass: 'single-line', columns: filterColumns(columns, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) },
  { cssClass: 'three-row', columns: filterColumns(columns, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) },
  { cssClass: 'single-column', columns: columns },
];

async function goTo(newOffset: number) {
  loading.value = true;
  error.value = null;
  const started = performance.now();
  try {
    const result = await viewSetFor(transport.value, backend.value).listPage({
      offset: Math.max(0, newOffset),
      limit: pageSize,
    });
    lastLoadMs.value = performance.now() - started;
    page.value = result;
    records.value = result.results;
    offset.value = result.offset;
  } catch (e: any) {
    // Both transports throw the same shape, so this branch needs no transport-specific handling.
    error.value = `Failed to load data: ${e.response?.status ?? ''} ${e.message}`;
  } finally {
    loading.value = false;
  }
}

async function runComparison() {
  benchmarking.value = true;
  results.value = [];
  try {
    for (const which of ['rest', 'muxws'] as Transport[]) {
      results.value = [...results.value, await runBenchmark(which, { backend: backend.value })];
    }
  } catch (e: any) {
    error.value = `Benchmark failed: ${e.message}`;
  } finally {
    benchmarking.value = false;
  }
}

// Switching transport reloads the same page, so the two are directly comparable on screen.
watch([transport, backend], () => goTo(offset.value), { immediate: false });
void goTo(0);
</script>

<style scoped>
.benchmark {
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
}
.benchmark th,
.benchmark td {
  padding: 0.15em 0.9em 0.15em 0;
  text-align: right;
}
.benchmark th:first-child,
.benchmark td:first-child {
  text-align: left;
}
.full-screen {
  position: fixed;
  inset: 0;
  z-index: 999;
  color: white;
  background: black;
}
.grid-class {
  height: 60em;
}
.full-screen .grid-class {
  flex: 1;
}
:deep(.df-grid.header) {
  font-weight: bold;
}
:deep(.df-grid.card.even) {
  background-color: #b0b0b040;
}
:deep(.df-grid.card.odd) {
  background-color: #60606040;
}
:deep(.df-grid.card) {
  display: grid;
  grid-template-columns: minmax(2em, 4em) repeat(3, auto) minmax(2em, 4em) minmax(2em, 8em);
  gap: .25em;

  padding: 0.5em;
  border: 1px solid #808080ff;
  border-radius: 6px;
  font-size: 0.85rem;
  /*
   * won't work for item measurements, so see the next selector adding negligible padding to parent. That seems to
   * finally take into account this margin
   */
  margin-bottom: .5em;
}
:deep(.df-grid.dynamic-scroller-item) {
  padding-bottom: .1px;
}
:deep(.df-grid.card.single-column) {
  grid-template-columns: auto;
}
:deep(.df-grid.card.single-column > *) {
  grid-column: 1 / 2 !important;
  grid-row: auto !important;
  grid-area: auto !important;
}
:deep(.df-grid.card.single-line) {
  /* column before last 1fr so that it stretches to remaining available space */
  grid-template-columns: repeat(9, minmax(min-content, max-content)) 1fr minmax(min-content, max-content);
}
:deep(.df-grid.card.single-line > *) {
  grid-column: auto !important;
  grid-row: auto !important;
  grid-area: auto !important;
}
:deep(.df-grid.cell) {
  border: 1px solid darkgray;
  border-radius: 4px;
  padding: 0 .25em;
}
:deep(.df-grid.cell.title), :deep(.df-grid.cell.artist), :deep(.df-grid.cell.genres) {
  grid-column: span 2;
}
:deep(.df-grid.cell.moods) {
  grid-column: 1 / 4;
  grid-row: 3;
}
:deep(.df-grid.cell.duration) {
  grid-column: 6;
}
:deep(.df-grid.cell.genres) {
  grid-column: 1 / 5;
  grid-row: 2;
}
:deep(.df-grid.cell.rating) {
  grid-column: 5;
  grid-row: 2;
}
:deep(.df-grid.cell.favorite) {
  text-align: center;
}
</style>
