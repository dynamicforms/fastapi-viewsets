import { expect, test, type Page } from '@playwright/test';

/**
 * The PostgreSQL (adorm) data source. `GET /backends` decides it: with adorm, its driver and a PostgreSQL server
 * (`DEMO_ADORM_DATABASE_URL`) the toggle is enabled and the grid lists, sorts and pages `/music-adorm`; otherwise the
 * toggle is disabled and its title is the reason.
 */

const PAGE_SIZE = 50;

interface DemoBackend {
  name: string;
  available: boolean;
  reason: string | null;
}

async function loadedCount(page: Page): Promise<number> {
  return Number(await page.getByTestId('loaded-count').innerText());
}

async function waitForFirstPage(page: Page) {
  await expect
    .poll(async () => loadedCount(page), { timeout: 30_000, message: 'no rows ever arrived' })
    .toBeGreaterThanOrEqual(PAGE_SIZE);
  await expect(page.getByTestId('error')).toHaveCount(0);
  await expect(page.locator('.df-grid.body-grid .df-anchored .df-grid.cell.id').first()).toBeVisible();
}

async function renderedIds(page: Page, count = 20): Promise<number[]> {
  const cells = await page.locator('.df-grid.body-grid .df-anchored .df-grid.cell.id').allInnerTexts();
  return cells
    .map((text) => text.trim())
    .filter((text) => text !== '')
    .map(Number)
    .filter((value) => !Number.isNaN(value))
    .slice(0, count);
}

async function adormBackend(page: Page): Promise<DemoBackend> {
  const response = await page.request.get('/backends');
  expect(response.ok()).toBe(true);
  const backends = (await response.json()) as DemoBackend[];
  const adorm = backends.find((backend) => backend.name === 'adorm');
  expect(adorm).toBeDefined();
  return adorm!;
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await waitForFirstPage(page);
});

test('the adorm toggle is disabled with the reason when adorm is not available', async ({ page }) => {
  const adorm = await adormBackend(page);
  test.skip(adorm.available, 'adorm is available in this run');

  const toggle = page.getByTestId('backend-adorm');
  await expect(toggle).toBeDisabled();
  await expect(toggle).toHaveAttribute('title', adorm.reason!);
});

test('the adorm backend serves the same records as the in-memory one, sorted and paged', async ({ page }) => {
  const adorm = await adormBackend(page);
  test.skip(!adorm.available, `adorm is not available: ${adorm.reason}`);
  const inMemory = await renderedIds(page);

  await page.getByTestId('backend-adorm').click();
  await waitForFirstPage(page);
  // Seeded from the same generator: same records, same order.
  expect(await renderedIds(page)).toEqual(inMemory);

  await page.locator('.df-grid.card.header .df-grid.cell.year').click();
  await expect.poll(async () => renderedIds(page), { timeout: 30_000 }).not.toEqual(inMemory);
  await expect(page.getByTestId('error')).toHaveCount(0);

  const before = await loadedCount(page);
  await page.locator('.df-grid.body-grid').first().hover();
  await expect
    .poll(async () => {
      await page.mouse.wheel(0, 4000);
      return loadedCount(page);
    }, { timeout: 30_000, message: 'scrolling never loaded a second page' })
    .toBeGreaterThan(before);
});
