import { expect, test, type Page } from '@playwright/test';

/**
 * End-to-end check of the demo: real browser, dev-server proxy, WebSocket and both backends,
 * covering the server start-up, the grid's `@load` wiring and the proxied WebSocket path.
 */

const PAGE_SIZE = 50;

async function loadedCount(page: Page): Promise<number> {
  return Number(await page.getByTestId('loaded-count').innerText());
}

/**
 * Waits for at least one page of rows (a tall window may load a second one) and fails if the app
 * shows an error.
 */
async function waitForFirstPage(page: Page) {
  await expect
    .poll(async () => loadedCount(page), { timeout: 30_000, message: 'no rows ever arrived' })
    .toBeGreaterThanOrEqual(PAGE_SIZE);
  await expect(page.getByTestId('error')).toHaveCount(0);
  // the grid draws the rows once it has settled on a layout, a moment after they arrived
  await expect(page.locator('.df-grid.body-grid .df-anchored .df-grid.cell.id').first()).toBeVisible();
}

/** Ids of the first `count` rendered rows, in display order; the grid virtualises, so only a prefix is stable. */
async function renderedIds(page: Page, count = 20): Promise<number[]> {
  // record cells only: the header and filter row use wrappers other than `.df-anchored`
  const cells = await page.locator('.df-grid.body-grid .df-anchored .df-grid.cell.id').allInnerTexts();
  return cells
    .map((text) => text.trim())
    // Empty first: Number('') is 0, so a blank id cell would count as record 0.
    .filter((text) => text !== '')
    .map(Number)
    .filter((value) => !Number.isNaN(value))
    .slice(0, count);
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await waitForFirstPage(page);
});

test('the first page arrives over REST', async ({ page }) => {
  expect(await renderedIds(page)).not.toHaveLength(0);
});

test('loading more appends rather than replacing', async ({ page }) => {
  // loadMore() concatenates; only a change of transport, backend, sort or filter starts over.
  const firstRows = await renderedIds(page);
  const before = await loadedCount(page);

  await page.locator('.df-grid.body-grid').first().hover();
  await expect
    .poll(async () => {
      await page.mouse.wheel(0, 4000);
      return loadedCount(page);
    }, { timeout: 30_000 })
    .toBeGreaterThan(before);

  await page.mouse.wheel(0, -100_000);
  await expect.poll(async () => renderedIds(page), { timeout: 15_000 }).toEqual(firstRows);
});

test('scrolling to the end loads the next page', async ({ page }) => {
  // Only the grid's @load asks for more rows, so this is the check that its wiring is live.
  const before = await loadedCount(page);
  await page.locator('.df-grid.body-grid').first().hover();
  await expect
    .poll(async () => {
      await page.mouse.wheel(0, 4000);
      return loadedCount(page);
    }, { timeout: 30_000, message: 'scrolling never triggered a second page' })
    .toBeGreaterThan(before);
});

test('the same data arrives over muxws', async ({ page }) => {
  // Loaded rows show that the server, the proxy and the codec all work.
  const overRest = await renderedIds(page);
  const loadedOverRest = await loadedCount(page);

  await page.getByTestId('transport-muxws').click();
  await waitForFirstPage(page);

  expect(await renderedIds(page)).toEqual(overRest);
  // Switching transport restarts paging, so the same number of pages has been fetched.
  expect(await loadedCount(page)).toBe(loadedOverRest);
});

test('the SQLite backend serves the same records as the in-memory one', async ({ page }) => {
  const inMemory = await renderedIds(page);

  await page.getByTestId('backend-db').click();
  await waitForFirstPage(page);

  // Both backends are seeded from the same generator: same records, same order.
  expect(await renderedIds(page)).toEqual(inMemory);
});

test('muxws against SQLite works, which is both new paths at once', async ({ page }) => {
  await page.getByTestId('transport-muxws').click();
  await waitForFirstPage(page);
  await page.getByTestId('backend-db').click();
  await waitForFirstPage(page);

  expect(await renderedIds(page)).not.toHaveLength(0);
});

test('sorting happens on the server and restarts the paging', async ({ page }) => {
  const ascending = await renderedIds(page);
  const loaded = await loadedCount(page);

  // `year`, as the default order is by id. `.card` picks the visible header, not the hidden measuring copy.
  await page.locator('.df-grid.card.header .df-grid.cell.year').click();
  await expect.poll(async () => renderedIds(page), { timeout: 30_000 }).not.toEqual(ascending);
  await expect(page.getByTestId('error')).toHaveCount(0);

  // A sort change restarts paging rather than appending.
  expect(await loadedCount(page)).toBeLessThanOrEqual(loaded);
});

test('the page loads without console errors', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(String(error)));

  await page.reload();
  await waitForFirstPage(page);
  await page.getByTestId('transport-muxws').click();
  await waitForFirstPage(page);

  expect(errors).toEqual([]);
});

test('every ViewSet agrees with the backend it is pointed at', async ({ page }) => {
  // the schema check reports a disagreement with one console.warn per ViewSet
  const mismatches: string[] = [];
  const schemas = new Set<string>();
  page.on('console', (message) => {
    if (message.text().includes('FE/BE definition mismatch')) mismatches.push(message.text());
  });
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname;
    if (path.endsWith('/schema')) schemas.add(path);
  });

  await page.reload();
  await waitForFirstPage(page);
  for (const [transport, backend] of [
    ['muxws', 'memory'],
    ['muxws', 'db'],
    ['rest', 'db'],
  ]) {
    await page.getByTestId(`transport-${transport}`).click();
    await page.getByTestId(`backend-${backend}`).click();
    await waitForFirstPage(page);
  }
  // the check runs in the background after construction
  await page.waitForTimeout(500);

  // the REST ViewSets fetched their schemas, so the check ran rather than failing silently
  expect([...schemas].sort()).toEqual(['/music-db/schema', '/music/schema']);
  expect(mismatches).toEqual([]);
});
