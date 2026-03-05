<template>
  <v-app>
    <v-main>
      <v-container fluid class="pa-4">
        <h1 class="text-h5 mb-4">Music Library</h1>
        <div v-if="error" class="text-error mb-4">{{ error }}</div>
        <div v-else-if="loading" class="text-center pa-8">Loading...</div>
        <df-grid
          v-else
          v-model:active-columns="activeColumnDef"
          :columns="columnsResponsive"
          :records="records"
          key-field="id"
          :show-filter-row="true"
          style="height: 80vh"
          @click="(data: any) => console.log('click:', data)"
          @sort="(data: any) => console.log('sort:', data)"
        />
      </v-container>
    </v-main>
  </v-app>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { createColumn, filterColumns, type ResponsiveColumnDefinitions } from '@dynamicforms/vue-grid';
import { musicTrackViewSet } from './viewsets';
import type { MusicTrack } from './viewsets';

const records = ref<MusicTrack[]>([]);
const loading = ref(true);
const error = ref<string | null>(null);
const activeColumnDef = ref('three-row');

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

onMounted(async () => {
  try {
    records.value = await musicTrackViewSet.list();
  } catch (e: any) {
    error.value = `Failed to load data: ${e.message}`;
  } finally {
    loading.value = false;
  }
});
</script>

<style scoped>
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
