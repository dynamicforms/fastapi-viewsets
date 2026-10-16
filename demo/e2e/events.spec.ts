import { expect, test, type BrowserContext, type Page } from '@playwright/test';

/**
 * Two tabs on one library. A Favorite toggled in one appears in the other: the backend's
 * `ChangeEvents` publishes `records_changed` to the viewset's topic, and the other tab's grid,
 * following it with `watch()`, retrieves the changed row and replaces it.
 *
 * Over REST the other tab learns of the change on its next poll, over muxws as a push.
 */

const FOLLOWING_TIMEOUT = 30_000;
/** One poll rung of the ladder is at most 6 s; a push arrives well within it. */
const CHANGE_TIMEOUT = 15_000;

/** Opens the demo on `transport` and `backend` and waits until its grid shows rows and follows its topic. */
async function openTab(
  context: BrowserContext,
  transport: 'rest' | 'muxws',
  backend: 'memory' | 'db' = 'memory',
): Promise<Page> {
  const page = await context.newPage();
  await page.goto('/');
  if (transport === 'muxws') await page.getByTestId('transport-muxws').click();
  if (backend === 'db') await page.getByTestId('backend-db').click();
  await expect(page.getByTestId('following')).toHaveAttribute('data-following', `${transport}:${backend}`, {
    timeout: FOLLOWING_TIMEOUT,
  });
  await expect(page.locator('.df-grid.body-grid .df-anchored .df-grid.cell.id').first()).toBeVisible();
  await expect(page.getByTestId('error')).toHaveCount(0);
  return page;
}

/** The Favorite cell of the record with `pk`, which renders as ✓ or ✗. */
function favoriteCell(page: Page, pk: string) {
  return page.locator(`.df-grid.body-grid .df-anchored:has(> .df-grid.card[data-pk="${pk}"]) .df-grid.cell.favorite`);
}

for (const [transport, backend] of [
  ['rest', 'memory'],
  ['muxws', 'memory'],
  ['rest', 'db'],
  ['muxws', 'db'],
] as const) {
  test(`a favorite toggled in one tab appears in the other over ${transport} on ${backend}`, async ({ context }) => {
    const editing = await openTab(context, transport, backend);
    const watching = await openTab(context, transport, backend);

    const pk = await editing.locator('.df-grid.body-grid .df-grid.card[data-pk]').first().getAttribute('data-pk');
    expect(pk).toBeTruthy();
    const before = (await favoriteCell(watching, pk!).innerText()).trim();
    const after = before === '✓' ? '✗' : '✓';

    await favoriteCell(editing, pk!).click();
    await expect(favoriteCell(editing, pk!)).toHaveText(after);
    await expect(favoriteCell(watching, pk!)).toHaveText(after, { timeout: CHANGE_TIMEOUT });

    // Toggled back, which also leaves the shared library as the other specs expect it.
    await favoriteCell(editing, pk!).click();
    await expect(favoriteCell(editing, pk!)).toHaveText(before);
    await expect(favoriteCell(watching, pk!)).toHaveText(before, { timeout: CHANGE_TIMEOUT });

    await expect(watching.getByTestId('error')).toHaveCount(0);
  });
}

/** The Favorite values of the first `count` rendered rows, in display order. */
async function firstFavorites(page: Page, count = 10): Promise<string[]> {
  const cells = await page.locator('.df-grid.body-grid .df-anchored .df-grid.cell.favorite').allInnerTexts();
  return cells.map((text) => text.trim()).slice(0, count);
}

test('in a list sorted by Favorite a toggled row leaves its place in both tabs', async ({ context }) => {
  const editing = await openTab(context, 'muxws');
  const watching = await openTab(context, 'muxws');

  // Sorted by favorite, the loaded prefix holds one value only. In pk order the values are mixed.
  for (const page of [editing, watching]) {
    await page.locator('.df-grid.card.header .df-grid.cell.favorite').click();
    await expect
      .poll(async () => new Set(await firstFavorites(page)).size, { timeout: CHANGE_TIMEOUT })
      .toBe(1);
  }
  const pk = await editing.locator('.df-grid.body-grid .df-grid.card[data-pk]').first().getAttribute('data-pk');
  expect(pk).toBeTruthy();
  const before = (await favoriteCell(editing, pk!).innerText()).trim();

  // The toggled value sorts after every row of the other one, so the row leaves the loaded prefix:
  // both tabs reload the list, and the rows they show again do not include it.
  await favoriteCell(editing, pk!).click();
  for (const page of [editing, watching]) {
    await expect
      .poll(async () => (await firstFavorites(page)).length > 0 && (await favoriteCell(page, pk!).count()) === 0, {
        timeout: CHANGE_TIMEOUT,
      })
      .toBe(true);
    expect(new Set(await firstFavorites(page))).toEqual(new Set([before]));
  }

  // Restored, which leaves the shared in-memory library as the other specs expect it.
  const restored = await editing.request.patch(`/music/${pk}`, { data: { favorite: before === '✓' } });
  expect(restored.ok()).toBe(true);
  await expect(watching.getByTestId('error')).toHaveCount(0);
});
