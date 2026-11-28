<template>
  <v-card variant="outlined" density="compact" class="files-panel pa-2 mb-4">
    <div class="d-flex align-center flex-wrap ga-3">
      <span class="text-subtitle-2">Files</span>
      <input ref="fileInput" type="file" data-testid="file-input" @change="onFileChosen" />
      <v-btn
        size="small"
        variant="tonal"
        :disabled="!chosen"
        :loading="uploading"
        data-testid="file-upload"
        @click="upload"
      >
        Upload
      </v-btn>
      <span v-if="progress !== null" class="text-caption">
        <span data-testid="file-upload-progress">{{ progress }}%</span>
        · <span data-testid="file-upload-events">{{ progressEvents }}</span> progress events
      </span>
      <span v-if="uploadResult" class="text-caption" data-testid="file-upload-result">{{ uploadResult }}</span>
      <v-btn size="small" variant="tonal" :loading="downloading" data-testid="file-download" @click="download(1)">
        PDF of track 1
      </v-btn>
      <v-btn size="small" variant="text" data-testid="file-download-missing" @click="download(MISSING_TRACK)">
        PDF of a missing track
      </v-btn>
      <span v-if="downloadResult" class="text-caption" data-testid="file-download-result">{{ downloadResult }}</span>
      <span v-if="fileError" class="text-caption text-error" data-testid="file-error">{{ fileError }}</span>
    </div>
  </v-card>
</template>

<script setup lang="ts">
import { ref } from 'vue';

import { errorBodyOf, isApiErrorBody } from '../../../vue/errors';
import type { FileDownload } from '../../../vue/file-download';

import { viewSetFor, type Backend, type Transport } from './viewsets';

const props = defineProps<{ transport: Transport; backend: Backend }>();

/** A track id no backend has: its PDF answers 404 `fastapi-viewsets.not_found`. */
const MISSING_TRACK = 999999999;

const fileInput = ref<HTMLInputElement | null>(null);
const chosen = ref<File | null>(null);
const uploading = ref(false);
/** The last reported upload progress as a whole percent; null before the first upload. */
const progress = ref<number | null>(null);
const progressEvents = ref(0);
const uploadResult = ref<string | null>(null);
const downloading = ref(false);
const downloadResult = ref<string | null>(null);
const fileError = ref<string | null>(null);

function onFileChosen() {
  chosen.value = fileInput.value?.files?.[0] ?? null;
}

/** The `detail_code` of the error's `ApiErrorBody`, otherwise the error's message. */
function errorText(error: unknown): string {
  const data = (error as { response?: { data?: unknown } } | null)?.response?.data;
  if (isApiErrorBody(data)) return errorBodyOf(error).detail_code;
  return error instanceof Error ? error.message : String(error);
}

async function upload() {
  const file = chosen.value;
  if (!file) return;
  uploading.value = true;
  fileError.value = null;
  uploadResult.value = null;
  progress.value = 0;
  progressEvents.value = 0;
  try {
    const received = await viewSetFor(props.transport, props.backend).uploadAttachment(file, (loaded, total) => {
      progressEvents.value += 1;
      progress.value = Math.round((100 * loaded) / (total || file.size || 1));
    });
    progress.value = 100;
    uploadResult.value = `${received.filename} ${received.size} bytes`;
  } catch (error) {
    fileError.value = errorText(error);
  } finally {
    uploading.value = false;
  }
}

/** Hands the downloaded file to the browser, which saves it under its `filename`. */
function save(download: FileDownload) {
  const url = URL.createObjectURL(download.blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = download.filename ?? 'download';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function download(pk: number) {
  downloading.value = true;
  fileError.value = null;
  downloadResult.value = null;
  try {
    const file = await viewSetFor(props.transport, props.backend).pdf(pk);
    save(file);
    downloadResult.value = `${file.filename} ${file.blob.size} bytes ${file.contentType}`;
  } catch (error) {
    fileError.value = errorText(error);
  } finally {
    downloading.value = false;
  }
}
</script>

<style scoped>
.files-panel {
  flex: 0 0 auto;
}
</style>
