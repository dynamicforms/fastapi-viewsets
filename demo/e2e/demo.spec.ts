import { expect, test, type Page } from '@playwright/test';

/**
 * "Does the demo actually work" - end to end, through a real browser, a real dev-server proxy, a
 * real WebSocket and two real backends.
 *
 * Every failure this is meant to catch has already happened once: a server that imported cleanly
 * and never started, a grid wired to an event the installed version did not emit, a WebSocket that
 * reported only that it never opened. None of them were visible to a unit test, because none of
 * them were about a unit.
 */

const PAGE_SIZE = 50;

async function loadedCount(page: Page): Promise<number> {
  return Number(await page.getByTestId('loaded-count').innerText());
}

/**
 * Waits for rows to land, and fails loudly if the app put an error on screen instead.
 *
 * At least a page rather than exactly one: the grid asks for more the moment the viewport nears
 * the end, so on a tall window a second page can legitimately arrive before this looks.
 */
async function waitForFirstPage(page: Page) {
  await expect
    .poll(async () => loadedCount(page), { timeout: 30_000, message: 'no rows ever arrived' })
    .toBeGreaterThanOrEqual(PAGE_SIZE);
  await expect(page.getByTestId('error')).toHaveCount(0);
}

/**
 * Ids of the first `count` rendered rows, in display order.
 *
 * A prefix rather than everything on screen: the grid virtualises, so how many rows exist in the
 * DOM at any moment depends on scroll position and window size and is not something to assert on.
 * The order of the first handful is.
 */
async function renderedIds(page: Page, count = 20): Promise<number[]> {
  const cells = await page.locator('.df-grid.card:not(.header) .df-grid.cell.id').allInnerTexts();
  return cells
    .map((text) => text.trim())
    // Empty first: Number('') is 0, so the filter row's blank id cell was arriving as record 0 and
    // sitting at the head of every comparison.
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
  // The distinction that matters for infinite scroll: loadMore() concatenates, and only an
  // explicit reload - a change of transport, backend, sort or filter - starts the list over.
  const firstRows = await renderedIds(page);
  const before = await loadedCount(page);

  await page.locator('.cards-grid').first().hover();
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
  // The grid emits @load when the viewport nears the end; nothing else asks for more rows, so this
  // is the only check that the wiring between the grid and the cursor is live.
  const before = await loadedCount(page);
  await page.locator('.df-grid.cards-grid, .cards-grid').first().hover();
  await expect
    .poll(async () => {
      await page.mouse.wheel(0, 4000);
      return loadedCount(page);
    }, { timeout: 30_000, message: 'scrolling never triggered a second page' })
    .toBeGreaterThan(before);
});

test('the same data arrives over muxws', async ({ page }) => {
  // The transport that reports failure as "the socket never opened", which says nothing about
  // whether the server, the proxy or the codec is at fault. Loading rows says all three are fine.
  const overRest = await renderedIds(page);
  const loadedOverRest = await loadedCount(page);

  await page.getByTestId('transport-muxws').click();
  await waitForFirstPage(page);

  expect(await renderedIds(page)).toEqual(overRest);
  // The loaded list, not the DOM window: switching transport restarts paging, so the same number
  // of pages must have been fetched. Asserted separately so a mismatch says which one it was.
  expect(await loadedCount(page)).toBe(loadedOverRest);
});

test('the SQLite backend serves the same records as the in-memory one', async ({ page }) => {
  const inMemory = await renderedIds(page);

  await page.getByTestId('backend-db').click();
  await waitForFirstPage(page);

  // The two are seeded from the same generator and claim to be interchangeable, so the same
  // records have to come back in the same order.
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

  // `year`, not `id`: the default order already is by id ascending, so sorting by it changes
  // nothing and the assertion below could never have been satisfied.
  await page.locator('.df-grid.header .df-grid.cell.year').click();
  await expect.poll(async () => renderedIds(page), { timeout: 30_000 }).not.toEqual(ascending);
  await expect(page.getByTestId('error')).toHaveCount(0);

  // A sort change invalidates any cursor in flight, so paging has to have restarted rather than
  // appended to what was already there.
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
