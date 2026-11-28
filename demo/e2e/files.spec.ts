import { readFile } from 'node:fs/promises';

import { expect, test, type Download, type Page } from '@playwright/test';

/**
 * Files panel of the demo: a multipart upload with progress and a PDF download, over the REST transport, and the
 * refusal of both by the muxws transport.
 */

const PAGE_SIZE = 50;
const UPLOAD_NAME = 'upload.bin';
const UPLOAD_SIZE = 3 * 1024 * 1024;
// bytes per second: a 3 MiB body takes about three seconds, enough for several progress events
const UPLOAD_THROUGHPUT = 1024 * 1024;
const PDF_FILENAME = 'track-1-é.pdf';

async function loadedCount(page: Page): Promise<number> {
  return Number(await page.getByTestId('loaded-count').innerText());
}

async function waitForFirstPage(page: Page) {
  await expect
    .poll(async () => loadedCount(page), { timeout: 30_000, message: 'no rows ever arrived' })
    .toBeGreaterThanOrEqual(PAGE_SIZE);
  await expect(page.getByTestId('error')).toHaveCount(0);
}

function uploadBody(): Buffer {
  const body = Buffer.alloc(UPLOAD_SIZE);
  for (let i = 0; i < body.length; i += 1) body[i] = (i * 31 + 7) % 256;
  return body;
}

async function chooseUploadFile(page: Page) {
  await page.getByTestId('file-input').setInputFiles({
    name: UPLOAD_NAME,
    mimeType: 'application/octet-stream',
    buffer: uploadBody(),
  });
}

async function switchToMuxws(page: Page) {
  await page.getByTestId('transport-muxws').click();
  await waitForFirstPage(page);
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await waitForFirstPage(page);
});

test.afterEach(async ({ page }) => {
  // the files panel reports its failures in its own element, never in the page's general error
  await expect(page.getByTestId('error')).toHaveCount(0);
});

test('a file uploaded over REST arrives whole and reports its progress', async ({ page }) => {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.emulateNetworkConditions', {
    offline: false,
    latency: 0,
    downloadThroughput: -1,
    uploadThroughput: UPLOAD_THROUGHPUT,
  });
  try {
    await chooseUploadFile(page);
    await page.getByTestId('file-upload').click();

    await expect(page.getByTestId('file-upload-result')).toHaveText(`${UPLOAD_NAME} ${UPLOAD_SIZE} bytes`, {
      timeout: 60_000,
    });
  } finally {
    await cdp.send('Network.emulateNetworkConditions', {
      offline: false,
      latency: 0,
      downloadThroughput: -1,
      uploadThroughput: -1,
    });
    await cdp.detach();
  }

  await expect(page.getByTestId('file-upload-progress')).toHaveText('100%');
  const events = Number((await page.getByTestId('file-upload-events').innerText()).trim());
  expect(events).toBeGreaterThanOrEqual(2);
  await expect(page.getByTestId('file-error')).toHaveCount(0);
});

test('a PDF downloaded over REST is saved under its decoded file name', async ({ page }) => {
  const downloadEvent = page.waitForEvent('download');
  await page.getByTestId('file-download').click();
  const download = await downloadEvent;

  expect(download.suggestedFilename()).toBe(PDF_FILENAME);
  const saved = await download.path();
  const content = await readFile(saved);
  expect(content.subarray(0, 5).toString('latin1')).toBe('%PDF-');

  const result = page.getByTestId('file-download-result');
  await expect(result).toHaveText(/^track-1-é\.pdf [1-9]\d* bytes application\/pdf/);
  const size = Number(/ (\d+) bytes /.exec(await result.innerText())?.[1]);
  expect(size).toBe(content.length);
  await expect(page.getByTestId('file-error')).toHaveCount(0);
});

test('the PDF of a missing track fails with the not-found code and saves nothing', async ({ page }) => {
  const downloads: Download[] = [];
  page.on('download', (download) => downloads.push(download));

  await page.getByTestId('file-download-missing').click();

  await expect(page.getByTestId('file-error')).toContainText('fastapi-viewsets.not_found', { timeout: 30_000 });
  await expect(page.getByTestId('file-download-result')).toHaveCount(0);
  expect(downloads).toHaveLength(0);
});

test('an upload over muxws is refused as unsupported by that transport', async ({ page }) => {
  await switchToMuxws(page);
  await chooseUploadFile(page);
  await page.getByTestId('file-upload').click();

  await expect(page.getByTestId('file-error')).toContainText(/muxws/i, { timeout: 30_000 });
  await expect(page.getByTestId('file-upload-result')).toHaveCount(0);
});

test('a download over muxws is refused as unsupported by that transport', async ({ page }) => {
  const downloads: Download[] = [];
  page.on('download', (download) => downloads.push(download));

  await switchToMuxws(page);
  await page.getByTestId('file-download').click();

  await expect(page.getByTestId('file-error')).toContainText(/muxws/i, { timeout: 30_000 });
  await expect(page.getByTestId('file-download-result')).toHaveCount(0);
  expect(downloads).toHaveLength(0);
});
