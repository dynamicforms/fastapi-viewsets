<template>
  <v-app>
    <v-main>
      <v-container fluid class="pa-4">
        <div class="d-flex align-center flex-wrap ga-4 mb-4">
          <h1 class="text-h5 mb-0">Music Library</h1>
          <v-btn-toggle v-model="transport" mandatory density="comfortable" color="primary">
            <v-btn value="rest" data-testid="transport-rest">REST</v-btn>
            <v-btn value="muxws" data-testid="transport-muxws">muxws</v-btn>
          </v-btn-toggle>
          <v-btn-toggle v-model="backend" mandatory density="comfortable" color="secondary">
            <v-btn value="memory" data-testid="backend-memory">in-memory</v-btn>
            <v-btn value="db" data-testid="backend-db">SQLite</v-btn>
          </v-btn-toggle>
          <v-spacer />
          <span class="text-caption">
            <span data-testid="loaded-count">{{ records.length }}</span> loaded<template
              v-if="!hasMore"
            > · end of list</template>
            <template v-if="lastLoadMs"> · last page {{ lastLoadMs.toFixed(0) }} ms</template>
          </span>
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
                <th /><th>min</th><th>p50</th><th>p95</th><th>max</th><th>total (ms)</th>
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

        <div v-if="error" data-testid="error" class="text-error mb-4">{{ error }}</div>

        <df-grid
          v-model:sort-state="sortState"
          v-model:active-columns="activeColumnDef"
          :columns="columnsResponsive"
          :records="records"
          :loading="loading"
          key-field="id"
          :show-filter-row="true"
          style="height: 70vh"
          @sort="onSort"
          @filter="onFilter"
          @load="loadMore"
        />
      </v-container>
    </v-main>
  </v-app>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import {
  createColumn,
  filterColumns,
  filterExternal,
  sortExternal,
  type GridFilterEvent,
  type GridSortEvent,
  type ResponsiveColumnDefinitions,
  type SortState,
} from '@dynamicforms/vue-grid';
import { runBenchmark, type BenchmarkResult } from './benchmark';
import { viewSetFor, type Backend, type MusicTrack, type Transport } from './viewsets';

const PAGE_SIZE = 50;

const transport = ref<Transport>('rest');
const backend = ref<Backend>('memory');

const records = ref<MusicTrack[]>([]);
const cursor = ref<string | null>(null);
const hasMore = ref(true);
const loading = ref(false);
const lastLoadMs = ref(0);
const error = ref<string | null>(null);
const activeColumnDef = ref('three-row');

const sortState = ref<SortState>([]);
const filterValues = ref<Record<string, unknown>>({});

const benchmarking = ref(false);
const results = ref<BenchmarkResult[]>([]);

/**
 * Which declared filter parameter each column maps to.
 *
 * The grid hands back a plain column → value map; the backend declares which operator each field
 * accepts, so `title` means `title__icontains` while `year` means an exact match. Anything not
 * listed here has no filter parameter on the backend and is dropped rather than guessed at.
 */
const FILTER_PARAM: Record<string, string> = {
  id: 'id',
  title: 'title__icontains',
  artist: 'artist__icontains',
  year: 'year',
  rating: 'rating',
  favorite: 'favorite',
  play_count: 'play_count',
  language: 'language__iexact',
  genres: 'genres__overlaps',
  moods: 'moods__overlaps',
};

// `as const` keeps `sortExternal`/`filterExternal` as their unique symbol types; in a plain object
// literal they widen to `symbol` and no longer match the sentinel the column definition expects.
const external = { sortable: { key: sortExternal }, filterable: { key: filterExternal } } as const;

const columns = [
  createColumn('id', 'Id', 'int', { cssClass: 'text-right', ...external }),
  createColumn('title', 'Title', 'plain', external),
  createColumn('artist', 'Artist', 'plain', external),
  createColumn('year', 'Year', 'int', { cssClass: 'text-right', ...external }),
  createColumn('duration', 'Duration', 'plain', { cssClass: 'text-right' }),
  createColumn('genres', 'Genres', 'plain', external),
  createColumn('rating', 'Rating', 'int', { cssClass: 'text-right', ...external }),
  createColumn('favorite', 'Favorite', 'checkbox', external),
  createColumn('play_count', 'Play count', 'int', { cssClass: 'text-right', ...external }),
  createColumn('moods', 'Moods', 'plain', external),
  createColumn('language', 'Language', 'plain', external),
];

const columnsResponsive: ResponsiveColumnDefinitions = [
  { cssClass: 'single-line', columns: filterColumns(columns, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) },
  { cssClass: 'three-row', columns: filterColumns(columns, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) },
  { cssClass: 'single-column', columns: columns },
];

/** Sort and filters as the backend's query parameters. Same on every page of a walk. */
function queryParams(): Record<string, any> {
  const params: Record<string, any> = {};
  const sort = sortState.value.map((c: SortState[number]) => `${c.columnName}:${c.direction}`).join(',');
  if (sort) params.sort = sort;
  for (const [column, value] of Object.entries(filterValues.value)) {
    const parameter = FILTER_PARAM[column];
    if (parameter && value !== null && value !== undefined && value !== '') params[parameter] = value;
  }
  return params;
}

/**
 * Fetches the first page and throws away whatever was loaded before.
 *
 * Any change to the sort or the filters has to restart here: a cursor is a position in one
 * particular ordering, and the backend refuses one issued for a different query rather than
 * silently returning a page from somewhere else.
 */
async function reload() {
  loading.value = true;
  error.value = null;
  records.value = [];
  cursor.value = null;
  hasMore.value = true;
  const started = performance.now();
  try {
    const page = await viewSetFor(transport.value, backend.value).listCursor({
      ...queryParams(),
      limit: PAGE_SIZE,
    });
    lastLoadMs.value = performance.now() - started;
    records.value = page.results;
    cursor.value = page.next;
    hasMore.value = page.hasMore;
  } catch (e: any) {
    // Both transports throw the same shape, so this needs no transport-specific handling.
    error.value = `Failed to load data: ${e.response?.status ?? ''} ${e.message}`;
  } finally {
    loading.value = false;
  }
}

/**
 * Appends the next page. The grid fires `@load` when the viewport nears the end and `loading` is
 * false, so setting `loading` for the duration of the fetch is what stops it firing again.
 */
async function loadMore() {
  if (loading.value || !cursor.value) return;
  loading.value = true;
  const started = performance.now();
  try {
    const page = await viewSetFor(transport.value, backend.value).listCursor({
      ...queryParams(),
      cursor: cursor.value,
      limit: PAGE_SIZE,
    });
    lastLoadMs.value = performance.now() - started;
    records.value = [...records.value, ...page.results];
    cursor.value = page.next;
    hasMore.value = page.hasMore;
  } catch (e: any) {
    error.value = `Failed to load more: ${e.response?.status ?? ''} ${e.message}`;
  } finally {
    loading.value = false;
  }
}

function onSort({ suggestedSort }: GridSortEvent) {
  sortState.value = suggestedSort;
  void reload();
}

function onFilter(event: GridFilterEvent) {
  filterValues.value = event.filterValues;
  void reload();
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

// Switching transport or backend reloads the same query, so the two are comparable on screen.
watch([transport, backend], () => void reload());
void reload();
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
