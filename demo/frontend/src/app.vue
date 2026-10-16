<template>
  <v-app>
    <v-main>
      <v-container fluid class="pa-4 page">
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
          <v-btn
            variant="text"
            size="small"
            href="/redoc"
            target="_blank"
            rel="noopener"
            prepend-icon="mdi-book-open-variant"
          >
            See API documentation
          </v-btn>
          <v-chip
            v-if="following"
            :data-following="following"
            size="small"
            color="success"
            variant="tonal"
            prepend-icon="mdi-access-point"
            data-testid="following"
          >
            following changes
          </v-chip>
          <span class="text-caption">
            <span data-testid="loaded-count">{{ records.length }}</span> loaded
            <template v-if="!hasMore">· end of list</template>
            <template v-if="lastLoadMs">· last page {{ lastLoadMs.toFixed(0) }} ms</template>
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
            Sequential cost is close on both — once a connection is warm, HTTP header parsing against muxws frame
            decoding is a small difference. The burst column is the real one: a browser opens about six concurrent
            HTTP/1.1 connections per host, so the seventh request waits. muxws multiplexes all of them onto one socket.
          </div>
        </v-alert>

        <div v-if="error" data-testid="error" class="text-error mb-4">{{ error }}</div>
        <div class="text-caption mb-2">
          Click a Favorite cell to toggle it. Every tab showing this library applies the change.
        </div>

        <df-grid
          v-model:sort-state="sortState"
          v-model:active-columns="activeColumnDef"
          :columns="columnsResponsive"
          :records="records"
          :loading="loading"
          :key-field="pkField"
          :show-filter-row="true"
          class="grid"
          @sort="onSort"
          @filter="onFilter"
          @load="loadMore"
          @click="onClick"
        />
      </v-container>
    </v-main>
  </v-app>
</template>

<script setup lang="ts">
import {
  createColumn,
  filterColumns,
  filterExternal,
  sortExternal,
  type GridClickEvent,
  type GridFilterEvent,
  type GridSortEvent,
  type ResponsiveColumnDefinitions,
  type SortState,
} from '@dynamicforms/vue-grid';
import { ref, watch } from 'vue';

import { toErrorDescription, type ApiErrorBody } from '../../../vue/errors';
import { applyChanges, type Change, type RecordsChanged } from '../../../vue/events';
import type { EventSubscription } from '../../../vue/events';

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

/** `transport:backend` of the ViewSet whose topic the grid follows, once its subscription is ready. */
const following = ref<string | null>(null);

const benchmarking = ref(false);
const results = ref<BenchmarkResult[]>([]);

// All four MusicTrack ViewSets share one pk field; read off an instance rather than repeated as a
// literal, so a model with a differently-named pk would not need this file edited to match.
const pkField = viewSetFor(transport.value, backend.value).pkFieldName;
const pkOf = (track: MusicTrack): number => track[pkField as keyof MusicTrack] as number;

/**
 * Which declared filter parameter each column maps to.
 *
 * The grid hands back a plain column → value map; the backend declares which operator each field
 * accepts, so `title` means `title__icontains` while `year` means an exact match. Anything not
 * listed here has no filter parameter on the backend and is dropped rather than guessed at.
 */
const FILTER_PARAM: Record<string, string> = {
  [pkField]: pkField,
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
  createColumn(pkField, 'Id', 'int', { cssClass: 'text-right', ...external }),
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

// `rows` is how many grid rows one record takes in that layout; the cells are placed within them in the styles below
const columnsResponsive: ResponsiveColumnDefinitions = [
  { cssClass: 'single-line', columns: filterColumns(columns, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) },
  { cssClass: 'three-row', rows: 3, columns: filterColumns(columns, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) },
  { cssClass: 'single-column', rows: columns.length, columns },
];

/**
 * The text of a failed call: the server's own `detail` and `code` where the response body names
 * one failure (both transports throw the same shape), the error's message otherwise.
 */
function failureText(what: string, e: any): string {
  const body = e?.response?.data;
  if (body && typeof body === 'object' && typeof body.detail === 'string') {
    const error = toErrorDescription(body as ApiErrorBody);
    return `${what}: ${error.detail}${error.code ? ` (${error.code})` : ''}`;
  }
  return `${what}: ${e?.response?.status ?? ''} ${e?.message ?? e}`;
}

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

/** Whether the list is sorted or filtered. Without either it is in pk order, and a created record goes last. */
function queryActive(): boolean {
  return Object.keys(queryParams()).length > 0;
}

/**
 * The page load in flight, or a settled promise. Never rejects: both loads report their failure
 * in `error`. A change is applied once it settles, so a page read before the change does not
 * overwrite it.
 */
let pageLoad: Promise<void> = Promise.resolve();

/**
 * Incremented each time the grid is emptied for a new first page. A page load drops its answer
 * when the count changed while it was in flight: the answer belongs to another ViewSet or another
 * query, and its cursor is one the shown ViewSet refuses.
 */
let generation = 0;

/** Empties the grid and shows it loading, until the next first page refills it. */
function clearRows() {
  generation += 1;
  loading.value = true;
  error.value = null;
  records.value = [];
  cursor.value = null;
  hasMore.value = true;
}

/**
 * Fetches the first page and throws away whatever was loaded before.
 *
 * Any change to the sort or the filters has to restart here: a cursor is a position in one
 * particular ordering, and the backend refuses one issued for a different query rather than
 * silently returning a page from somewhere else.
 */
function reload(): Promise<void> {
  pageLoad = firstPage();
  return pageLoad;
}

async function firstPage() {
  clearRows();
  const own = generation;
  const started = performance.now();
  try {
    const page = await viewSetFor(transport.value, backend.value).listCursor({
      ...queryParams(),
      limit: PAGE_SIZE,
    });
    if (own !== generation) return;
    lastLoadMs.value = performance.now() - started;
    records.value = page.results;
    cursor.value = page.next;
    hasMore.value = page.hasMore;
  } catch (e: any) {
    if (own === generation) error.value = failureText('Failed to load data', e);
  } finally {
    if (own === generation) loading.value = false;
  }
}

/**
 * Appends the next page. The grid fires `@load` when the viewport nears the end and `loading` is
 * false, so setting `loading` for the duration of the fetch is what stops it firing again.
 */
function loadMore() {
  if (loading.value || !cursor.value) return;
  pageLoad = nextPage(cursor.value);
}

async function nextPage(from: string) {
  const own = generation;
  loading.value = true;
  const started = performance.now();
  try {
    const page = await viewSetFor(transport.value, backend.value).listCursor({
      ...queryParams(),
      cursor: from,
      limit: PAGE_SIZE,
    });
    if (own !== generation) return;
    lastLoadMs.value = performance.now() - started;
    records.value = [...records.value, ...page.results];
    cursor.value = page.next;
    hasMore.value = page.hasMore;
  } catch (e: any) {
    if (own === generation) error.value = failureText('Failed to load more', e);
  } finally {
    if (own === generation) loading.value = false;
  }
}

/**
 * The JSON of each record this tab saved, until the `records_changed` of that save arrives. The
 * grid already shows such a record, so its own event changes nothing.
 */
const saved = new Map<number, string>();

/** Whether `item` is the event of this tab's own save, which is forgotten once it is seen. */
function isOwnSave(item: Change<MusicTrack, number>): boolean {
  const expected = saved.get(item.pk);
  if (expected === undefined) return false;
  saved.delete(item.pk);
  return expected === JSON.stringify(item.record);
}

/**
 * Applies one `records_changed` of the shown ViewSet to the loaded rows.
 *
 * The grid holds a prefix of the server's list. Without a sort or a filter that list is in pk
 * order: a change to a loaded row replaces or removes it, a created record is appended once the
 * last page is loaded and otherwise arrives with the page that holds it, and an updated row outside
 * the prefix is left to its page as well.
 *
 * With a sort or a filter, a created or updated record can belong anywhere in the list, or no
 * longer belong in it. The grid cannot place it, so it reloads the list; a deleted row is removed.
 */
function applyChange(change: RecordsChanged<MusicTrack, number>) {
  const changes = change.op === 'deleted' ? change.changes : change.changes.filter((item) => !isOwnSave(item));
  if (!changes.length) return;
  if (change.op !== 'deleted' && queryActive()) {
    void reload();
    return;
  }
  const loaded = new Set(records.value.map(pkOf));
  const applied = change.op === 'created' && !hasMore.value ? changes : changes.filter((item) => loaded.has(item.pk));
  records.value = applyChanges(records.value, { op: change.op, changes: applied }, pkField);
}

let watching: EventSubscription | undefined;

/**
 * Follows the shown ViewSet's topic, then loads its first page. Subscribed first, so a change made
 * between the read and the subscription is not missed.
 *
 * `fetch: true` retrieves each created and updated record before `onChange`: the backend's
 * `ChangeEvents` sends pks only. A resync, after the client missed events it cannot recover from
 * the backend's log, reloads the list.
 */
async function followAndReload() {
  watching?.unsubscribe();
  following.value = null;
  saved.clear();
  // The rows of the previous ViewSet go at once, not after the subscription is ready.
  clearRows();
  const which = `${transport.value}:${backend.value}`;
  const subscription = viewSetFor(transport.value, backend.value).watch({
    fetch: true,
    onChange(change) {
      void pageLoad.then(() => {
        if (watching === subscription) applyChange(change);
      });
    },
    onResync() {
      void reload();
    },
  });
  watching = subscription;
  try {
    await subscription.ready;
    if (watching === subscription) following.value = which;
  } catch {
    // The grid still works without following; the chip stays off.
  }
  if (watching === subscription) await reload();
}

/**
 * A click on a Favorite cell toggles that track's `favorite` on the shown ViewSet. The
 * `records_changed` the backend publishes for it reaches every tab that follows the topic.
 *
 * In this tab the answer replaces the row at once, or, with a sort or a filter, the list reloads:
 * the toggled row may have moved or left the list. An answer that arrives after a switch to another
 * ViewSet is dropped.
 */
async function onClick({ columnName, rowData }: GridClickEvent) {
  if (columnName !== 'favorite' || !rowData) return;
  const track = rowData as unknown as MusicTrack;
  const shownTransport = transport.value;
  const shownBackend = backend.value;
  const isShown = () => transport.value === shownTransport && backend.value === shownBackend;
  try {
    const answer = await viewSetFor(shownTransport, shownBackend).partialUpdate(pkOf(track), {
      favorite: !track.favorite,
    });
    if (!isShown()) return;
    saved.set(pkOf(answer), JSON.stringify(answer));
    if (queryActive()) {
      void reload();
      return;
    }
    records.value = applyChanges(
      records.value,
      { op: 'updated', changes: [{ pk: pkOf(answer), record: answer }] },
      pkField,
    );
  } catch (e: any) {
    if (isShown()) error.value = failureText('Failed to save', e);
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
    error.value = failureText('Benchmark failed', e);
  } finally {
    benchmarking.value = false;
  }
}

// Switching transport or backend follows the other ViewSet and reloads the same query, so the two
// are comparable on screen.
watch([transport, backend], () => void followAndReload());
void followAndReload();
</script>

<style scoped>
/* the grid takes whatever height the toolbar and the benchmark results leave in the window */
.page {
  display: flex;
  flex-direction: column;
  height: 100dvh;
}
.grid {
  flex: 1 1 0;
  min-height: 0;
}
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
/*
 * Every record's cells are items of one grid shared by the body, the header and the filter row, so the grid itself is
 * declared on .df-record-grid. .df-grid.card is the row-anchor: a box spanning the record's band that carries its
 * background and border, beside the cells rather than around them.
 */
:deep(.df-record-grid) {
  display: grid;
  gap: 0.25em;
  font-size: 0.85rem;
}
:deep(.df-grid.card) {
  border: 1px solid #808080ff;
  border-radius: 6px;
}
:deep(.df-grid.cell) {
  border: 1px solid darkgray;
  border-radius: 4px;
  padding: 0 0.25em;
}
:deep(.df-grid.cell.favorite) {
  text-align: center;
}

/*
 * Cells are placed within their own record's band: --row-base is the first grid row of the record, published by the
 * grid, so every placement is relative to it rather than an absolute row.
 */

/* single-line: one row, the eleven fields in declaration order */
:deep(.df-record-grid.single-line) {
  grid-template-columns: repeat(9, minmax(min-content, max-content)) 1fr minmax(min-content, max-content);
}
:deep(.df-record-grid.single-line .df-grid.cell) {
  grid-row: calc(var(--row-base) + 1);
}
:deep(.df-record-grid.single-line .df-grid.cell.id) {
  grid-column: 1;
}
:deep(.df-record-grid.single-line .df-grid.cell.title) {
  grid-column: 2;
}
:deep(.df-record-grid.single-line .df-grid.cell.artist) {
  grid-column: 3;
}
:deep(.df-record-grid.single-line .df-grid.cell.year) {
  grid-column: 4;
}
:deep(.df-record-grid.single-line .df-grid.cell.duration) {
  grid-column: 5;
}
:deep(.df-record-grid.single-line .df-grid.cell.genres) {
  grid-column: 6;
}
:deep(.df-record-grid.single-line .df-grid.cell.rating) {
  grid-column: 7;
}
:deep(.df-record-grid.single-line .df-grid.cell.favorite) {
  grid-column: 8;
}
:deep(.df-record-grid.single-line .df-grid.cell.play_count) {
  grid-column: 9;
}
:deep(.df-record-grid.single-line .df-grid.cell.moods) {
  grid-column: 10;
}
:deep(.df-record-grid.single-line .df-grid.cell.language) {
  grid-column: 11;
}

/* three-row: id, title, artist, year / genres, rating, duration / moods, favorite, play count, language */
:deep(.df-record-grid.three-row) {
  grid-template-columns: minmax(2em, 4em) repeat(3, auto) minmax(2em, 4em) minmax(2em, 8em);
}
:deep(.df-record-grid.three-row .df-grid.cell.id) {
  grid-column: 1;
  grid-row: calc(var(--row-base) + 1);
}
:deep(.df-record-grid.three-row .df-grid.cell.title) {
  grid-column: 2 / 4;
  grid-row: calc(var(--row-base) + 1);
}
:deep(.df-record-grid.three-row .df-grid.cell.artist) {
  grid-column: 4 / 6;
  grid-row: calc(var(--row-base) + 1);
}
:deep(.df-record-grid.three-row .df-grid.cell.year) {
  grid-column: 6;
  grid-row: calc(var(--row-base) + 1);
}
:deep(.df-record-grid.three-row .df-grid.cell.genres) {
  grid-column: 1 / 5;
  grid-row: calc(var(--row-base) + 2);
}
:deep(.df-record-grid.three-row .df-grid.cell.rating) {
  grid-column: 5;
  grid-row: calc(var(--row-base) + 2);
}
:deep(.df-record-grid.three-row .df-grid.cell.duration) {
  grid-column: 6;
  grid-row: calc(var(--row-base) + 2);
}
:deep(.df-record-grid.three-row .df-grid.cell.moods) {
  grid-column: 1 / 4;
  grid-row: calc(var(--row-base) + 3);
}
:deep(.df-record-grid.three-row .df-grid.cell.favorite) {
  grid-column: 4;
  grid-row: calc(var(--row-base) + 3);
}
:deep(.df-record-grid.three-row .df-grid.cell.play_count) {
  grid-column: 5;
  grid-row: calc(var(--row-base) + 3);
}
:deep(.df-record-grid.three-row .df-grid.cell.language) {
  grid-column: 6;
  grid-row: calc(var(--row-base) + 3);
}

/* single-column: one field per row, in declaration order */
:deep(.df-record-grid.single-column) {
  grid-template-columns: auto;
}
:deep(.df-record-grid.single-column .df-grid.cell) {
  grid-column: 1;
}
:deep(.df-record-grid.single-column .df-grid.cell.id) {
  grid-row: calc(var(--row-base) + 1);
}
:deep(.df-record-grid.single-column .df-grid.cell.title) {
  grid-row: calc(var(--row-base) + 2);
}
:deep(.df-record-grid.single-column .df-grid.cell.artist) {
  grid-row: calc(var(--row-base) + 3);
}
:deep(.df-record-grid.single-column .df-grid.cell.year) {
  grid-row: calc(var(--row-base) + 4);
}
:deep(.df-record-grid.single-column .df-grid.cell.duration) {
  grid-row: calc(var(--row-base) + 5);
}
:deep(.df-record-grid.single-column .df-grid.cell.genres) {
  grid-row: calc(var(--row-base) + 6);
}
:deep(.df-record-grid.single-column .df-grid.cell.rating) {
  grid-row: calc(var(--row-base) + 7);
}
:deep(.df-record-grid.single-column .df-grid.cell.favorite) {
  grid-row: calc(var(--row-base) + 8);
}
:deep(.df-record-grid.single-column .df-grid.cell.play_count) {
  grid-row: calc(var(--row-base) + 9);
}
:deep(.df-record-grid.single-column .df-grid.cell.moods) {
  grid-row: calc(var(--row-base) + 10);
}
:deep(.df-record-grid.single-column .df-grid.cell.language) {
  grid-row: calc(var(--row-base) + 11);
}
</style>
