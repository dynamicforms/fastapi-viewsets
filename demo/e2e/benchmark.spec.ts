import { expect, test, type Locator, type Page } from '@playwright/test';

/**
 * The Benchmark tab: setup card, controls, progress, results table, chart and API errors. `GET /benchmark` decides
 * what runs: without a PostgreSQL database the benchmark is unavailable and only its reason is checked; Celery
 * executors without their workers are reported unavailable. Runs are kept short (1 s, few cells).
 */

const EXECUTORS = ['fastapi', 'classic', 'hybrid'] as const;
const DB_APIS = ['django', 'adorm'] as const;
const TRANSPORTS = ['rest', 'muxws'] as const;
const LEVELS = [10, 100, 1000] as const;

type Executor = (typeof EXECUTORS)[number];
type DbApi = (typeof DB_APIS)[number];
type Transport = (typeof TRANSPORTS)[number];
type Level = (typeof LEVELS)[number];

interface ExecutorState {
  available: boolean;
  reason: string | null;
}

interface BenchmarkState {
  available: boolean;
  reason: string | null;
  db_latency_ms: number | null;
  latency_proxy: boolean;
  adorm_available: boolean;
  adorm_reason: string | null;
  adorm_pool: number;
  executors: Record<Executor, ExecutorState>;
  bench_processes: number;
  loadgen_processes: number;
}

interface Selection {
  transports: Transport[];
  executors: Executor[];
  dbApis: DbApi[];
  levels: Level[];
  operation?: 'retrieve' | 'list';
  durationS?: number;
  latencyMs?: number;
  /** A preset button to click instead of typing `latencyMs`. */
  latencyPreset?: 0 | 2 | 10 | 50;
}

const CELL_ATTRIBUTES = ['data-throughput', 'data-p50', 'data-p95', 'data-p99', 'data-error-rate'] as const;

/** `executors` as a map by name, whether the API answers a map or a list of `{name, ...}`. */
function executorsByName(raw: unknown): Record<Executor, ExecutorState> {
  if (Array.isArray(raw)) {
    const entries = (raw as (ExecutorState & { name: Executor })[]).map((executor) => [executor.name, executor]);
    return Object.fromEntries(entries) as unknown as Record<Executor, ExecutorState>;
  }
  return raw as Record<Executor, ExecutorState>;
}

async function benchmarkState(page: Page): Promise<BenchmarkState> {
  const response = await page.request.get('/benchmark');
  expect(response.ok()).toBe(true);
  const state = (await response.json()) as BenchmarkState;
  return { ...state, executors: executorsByName(state.executors) };
}

interface Needs {
  executors?: Executor[];
  adorm?: boolean;
  latencyProxy?: boolean;
}

async function skipUnlessAvailable(page: Page, needs: Needs = {}): Promise<BenchmarkState> {
  const state = await benchmarkState(page);
  test.skip(!state.available, `the benchmark is not available: ${state.reason}`);
  for (const executor of needs.executors ?? []) {
    const { available, reason } = state.executors[executor];
    test.skip(!available, `executor ${executor} is not available: ${reason}`);
  }
  test.skip(!!needs.adorm && !state.adorm_available, `adorm is not available: ${state.adorm_reason}`);
  test.skip(!!needs.latencyProxy && !state.latency_proxy, 'no latency proxy runs');
  return state;
}

/** The form control a test id names: the element itself or the input inside its wrapper. */
async function control(page: Page, testId: string, selector: string): Promise<Locator> {
  const element = page.getByTestId(testId);
  const inner = element.locator(selector);
  return (await inner.count()) > 0 ? inner.first() : element;
}

async function setCheckbox(page: Page, testId: string, checked: boolean) {
  const input = await control(page, testId, 'input[type="checkbox"]');
  await input.setChecked(checked, { force: true });
}

async function setNumber(page: Page, testId: string, value: number) {
  const input = await control(page, testId, 'input');
  await input.fill(String(value));
  await input.blur();
}

async function selectOperation(page: Page, operation: 'retrieve' | 'list') {
  const element = page.getByTestId('bench-operation');
  const native = (await element.evaluate((node) => node.tagName)) === 'SELECT' ? element : element.locator('select');
  if ((await native.count()) > 0) {
    await native.first().selectOption(operation);
    return;
  }
  await element.click();
  await page.getByRole('option', { name: new RegExp(`^${operation}$`, 'i') }).click();
}

async function configure(page: Page, selection: Selection) {
  for (const transport of TRANSPORTS) {
    await setCheckbox(page, `bench-transport-${transport}`, selection.transports.includes(transport));
  }
  for (const executor of EXECUTORS) {
    const checkbox = await control(page, `bench-executor-${executor}`, 'input[type="checkbox"]');
    // an unavailable executor's checkbox may be disabled; it only has to be off
    if (selection.executors.includes(executor) || (await checkbox.isEnabled())) {
      await setCheckbox(page, `bench-executor-${executor}`, selection.executors.includes(executor));
    }
  }
  for (const dbApi of DB_APIS) {
    const checkbox = await control(page, `bench-db-${dbApi}`, 'input[type="checkbox"]');
    // an unavailable DB API's checkbox is disabled and off
    if (selection.dbApis.includes(dbApi) || (await checkbox.isEnabled())) {
      await setCheckbox(page, `bench-db-${dbApi}`, selection.dbApis.includes(dbApi));
    }
  }
  for (const level of LEVELS) {
    await setCheckbox(page, `bench-level-${level}`, selection.levels.includes(level));
  }
  await selectOperation(page, selection.operation ?? 'retrieve');
  await setNumber(page, 'bench-duration', selection.durationS ?? 1);
  const latency = await control(page, 'bench-latency', 'input');
  if (selection.latencyPreset !== undefined) {
    await page.getByTestId(`bench-latency-preset-${selection.latencyPreset}`).click();
    await expect(latency).toHaveValue(String(selection.latencyPreset));
  } else if ((selection.latencyMs ?? 0) !== 0 || (await latency.isEnabled())) {
    // without a latency proxy the field is disabled and stays 0
    await setNumber(page, 'bench-latency', selection.latencyMs ?? 0);
  }
}

function cell(page: Page, executor: Executor, dbApi: DbApi, transport: Transport, level: Level): Locator {
  return page.getByTestId(`bench-cell-${executor}-${dbApi}-${transport}-${level}`);
}

/** Result cells that carry a measured throughput. */
async function measuredCells(page: Page): Promise<number> {
  const values = await page
    .locator('[data-testid^="bench-cell-"]')
    .evaluateAll((cells) => cells.map((node) => node.getAttribute('data-throughput')));
  return values.filter((value) => value !== null && value !== '' && Number.isFinite(Number(value))).length;
}

async function expectMeasured(target: Locator) {
  await expect(target).toHaveAttribute('data-throughput', /^\d/, { timeout: 60_000 });
  const values: Record<string, number> = {};
  for (const attribute of CELL_ATTRIBUTES) {
    const raw = await target.getAttribute(attribute);
    expect(raw, `${attribute} is set`).not.toBeNull();
    values[attribute] = Number(raw);
    expect(Number.isFinite(values[attribute]), `${attribute}=${raw} is a number`).toBe(true);
  }
  expect(values['data-throughput']).toBeGreaterThan(0);
  expect(values['data-p50']).toBeGreaterThan(0);
  expect(values['data-p50']).toBeLessThanOrEqual(values['data-p95']);
  expect(values['data-p95']).toBeLessThanOrEqual(values['data-p99']);
  expect(values['data-error-rate']).toBeGreaterThanOrEqual(0);
  expect(values['data-error-rate']).toBeLessThanOrEqual(100);
  return values;
}

async function waitUntilIdle(page: Page) {
  await expect(page.getByTestId('bench-run')).toBeEnabled({ timeout: 120_000 });
}

function runBody(overrides: Record<string, unknown> = {}) {
  return {
    transport: 'rest',
    executor: 'fastapi',
    db_api: 'adorm',
    operation: 'retrieve',
    concurrency: 10,
    duration_s: 1,
    db_latency_ms: 0,
    ...overrides,
  };
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('tab-benchmark').click();
  await expect(page.getByTestId('benchmark-setup')).toBeVisible();
});

test('the setup card states availability, workers, load generator processes and the adorm pool', async ({ page }) => {
  const state = await benchmarkState(page);
  const setup = page.getByTestId('benchmark-setup');

  if (!state.available) {
    await expect(setup).toContainText(state.reason!);
    return;
  }
  await expect(setup).toContainText(String(state.adorm_pool));
  const adorm = await control(page, 'bench-db-adorm', 'input[type="checkbox"]');
  if (state.adorm_available) {
    await expect(page.getByTestId('adorm-unavailable')).toHaveCount(0);
    await expect(adorm).toBeEnabled();
  } else {
    await expect(page.getByTestId('adorm-unavailable')).toContainText(state.adorm_reason!);
    await expect(adorm).toBeDisabled();
    await expect(adorm).not.toBeChecked();
  }
  const latency = await control(page, 'bench-latency', 'input');
  if (state.latency_proxy) {
    await expect(latency).toBeEnabled();
  } else {
    await expect(setup).toContainText('no latency proxy');
    await expect(latency).toBeDisabled();
    await expect(page.getByTestId('bench-latency-preset-10')).toBeDisabled();
  }
  for (const executor of EXECUTORS) {
    const line = page.getByTestId(`executor-${executor}`);
    await expect(line).toBeVisible();
    const { available, reason } = state.executors[executor];
    if (available) {
      await expect(line).toContainText(/\d+ active/);
    } else {
      await expect(line).toContainText(reason!);
    }
  }
  expect(state.bench_processes).toBeGreaterThanOrEqual(1);
  await expect(page.getByTestId('executor-fastapi')).toContainText(`${state.bench_processes} uvicorn process`);
  if (state.executors.fastapi.available) {
    await expect(page.getByTestId('executor-fastapi')).toContainText(/[1-9]\d* active/);
  }
  expect(state.loadgen_processes).toBeGreaterThanOrEqual(1);
  const loadgen = page.getByTestId('benchmark-loadgen-processes');
  await expect(loadgen).toContainText(`Load generator: ${state.loadgen_processes} process`);
  await expect(loadgen).toHaveAttribute('data-processes', String(state.loadgen_processes));
  if (state.executors.hybrid.available) {
    await expect(page.getByTestId('executor-hybrid')).toContainText('200');
    // 2 prefork + 2 async processes
    await expect(page.getByTestId('executor-hybrid')).toContainText(/[1-9]\d* active/);
  }
  if (state.executors.classic.available) {
    await expect(page.getByTestId('executor-classic')).toContainText(/[1-9]\d* active/);
  }
});

test('an unavailable executor is reported with its reason and refused by the API', async ({ page }) => {
  const state = await skipUnlessAvailable(page);
  const unavailable = EXECUTORS.filter((executor) => !state.executors[executor].available);
  test.skip(unavailable.length === 0, 'every executor is available in this run');

  for (const executor of unavailable) {
    await expect(page.getByTestId(`executor-${executor}`)).toContainText(state.executors[executor].reason!);
    const response = await page.request.post('/benchmark/runs', { data: runBody({ executor, db_api: 'django' }) });
    expect(response.status()).toBe(503);
    const body = (await response.json()) as { detail: string; detail_code: string };
    expect(body.detail_code).toEqual(expect.any(String));
  }
});

test('a run fills one cell per executor, DB API, transport and level, with progress and chart', async ({ page }) => {
  test.setTimeout(240_000);
  await skipUnlessAvailable(page, { executors: ['fastapi'], adorm: true, latencyProxy: true });

  await configure(page, {
    transports: ['rest', 'muxws'],
    executors: ['fastapi'],
    dbApis: ['django', 'adorm'],
    levels: [10],
    operation: 'retrieve',
    durationS: 1,
    latencyMs: 2,
  });
  await page.getByTestId('bench-run').click();
  await expect(page.getByTestId('bench-progress')).toContainText(/cell \d+ of 4/, { timeout: 30_000 });
  await expect(page.getByTestId('bench-progress')).toContainText('c=10');
  await waitUntilIdle(page);

  await expect(page.getByTestId('bench-error')).toHaveCount(0);
  await expect(page.getByTestId('bench-results')).toBeVisible();
  for (const dbApi of DB_APIS) {
    for (const transport of TRANSPORTS) {
      const values = await expectMeasured(cell(page, 'fastapi', dbApi, transport, 10));
      expect(values['data-error-rate'], `${dbApi} over ${transport} has no errors`).toBe(0);
    }
  }
  await expect(page.getByTestId('bench-chart').first()).toBeVisible();
});

test('the list operation runs with an injected latency', async ({ page }) => {
  test.setTimeout(120_000);
  const state = await skipUnlessAvailable(page, { executors: ['fastapi'], adorm: true, latencyProxy: true });

  await configure(page, {
    transports: ['rest'],
    executors: ['fastapi'],
    dbApis: ['adorm'],
    levels: [10],
    operation: 'list',
    durationS: 1,
    latencyPreset: 10,
  });
  await expect(page.getByTestId('bench-latency-preset-10')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('bench-latency-preset-0')).toHaveAttribute('aria-pressed', 'false');
  await page.getByTestId('bench-run').click();
  await waitUntilIdle(page);

  const values = await expectMeasured(cell(page, 'fastapi', 'adorm', 'rest', 10));
  await expect(page.getByTestId('bench-loadgen')).toBeVisible();
  await expect(page.getByTestId('bench-loadgen')).toContainText(`busiest process of ${state.loadgen_processes}`);
  await expect(page.getByTestId('bench-loadgen')).toContainText('s CPU');
  // one injected 10 ms round trip at least
  expect(values['data-p50']).toBeGreaterThanOrEqual(10);
  expect((await benchmarkState(page)).db_latency_ms).toBe(10);
});

test('both Celery executors run both DB APIs', async ({ page }) => {
  test.setTimeout(240_000);
  await skipUnlessAvailable(page, { executors: ['classic', 'hybrid'], adorm: true });

  await configure(page, {
    transports: ['rest'],
    executors: ['classic', 'hybrid'],
    dbApis: ['django', 'adorm'],
    levels: [10],
    durationS: 1,
    latencyMs: 0,
  });
  await page.getByTestId('bench-run').click();
  await waitUntilIdle(page);

  await expect(page.getByTestId('bench-error')).toHaveCount(0);
  for (const executor of ['classic', 'hybrid'] as const) {
    for (const dbApi of DB_APIS) {
      await expectMeasured(cell(page, executor, dbApi, 'rest', 10));
    }
  }
});

test('Stop ends the run after the current cell and Clear empties the results', async ({ page }) => {
  test.setTimeout(180_000);
  await skipUnlessAvailable(page, { executors: ['fastapi'], adorm: true });

  await configure(page, {
    transports: ['rest'],
    executors: ['fastapi'],
    dbApis: ['django', 'adorm'],
    levels: [10, 100, 1000],
    durationS: 1,
    latencyMs: 0,
  });
  await page.getByTestId('bench-run').click();
  await expect(page.getByTestId('bench-progress')).toContainText(/cell 1 of 6/, { timeout: 30_000 });
  await page.getByTestId('bench-stop').click();
  await waitUntilIdle(page);

  const measured = await measuredCells(page);
  expect(measured).toBeGreaterThanOrEqual(1);
  expect(measured).toBeLessThan(6);
  await expectMeasured(cell(page, 'fastapi', 'django', 'rest', 10));

  await page.getByTestId('bench-clear').click();
  await expect.poll(async () => measuredCells(page)).toBe(0);
});

test('a run refused while another is in progress shows the 409 in bench-error', async ({ page }) => {
  test.setTimeout(120_000);
  await skipUnlessAvailable(page, { executors: ['fastapi'], adorm: true });

  await configure(page, {
    transports: ['rest'],
    executors: ['fastapi'],
    dbApis: ['adorm'],
    levels: [10],
    durationS: 1,
    latencyMs: 0,
  });
  // keeps the server busy for about 5 s (warm-up plus duration plus draining)
  const busy = page.request.post('/benchmark/runs', { data: runBody({ duration_s: 4 }), timeout: 60_000 });
  await page.waitForTimeout(500);

  const refused = await page.request.post('/benchmark/runs', { data: runBody() });
  expect(refused.status()).toBe(409);
  expect(((await refused.json()) as { detail_code: string }).detail_code).toEqual(expect.any(String));

  await page.getByTestId('bench-run').click();
  await expect(page.getByTestId('bench-error')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId('bench-error')).not.toBeEmpty();

  expect((await busy).status()).toBe(200);
  await waitUntilIdle(page);
});

test('invalid run parameters are answered with 422 and field errors', async ({ page }) => {
  await skipUnlessAvailable(page);

  for (const invalid of [{ duration_s: 31 }, { duration_s: 0 }, { db_latency_ms: 101 }, { executor: 'nope' }]) {
    const response = await page.request.post('/benchmark/runs', { data: runBody(invalid) });
    expect(response.status(), JSON.stringify(invalid)).toBe(422);
    const body = (await response.json()) as { detail_code: string; field_errors: unknown };
    expect(body.detail_code).toEqual(expect.any(String));
    expect(body.field_errors).toBeTruthy();
  }
});

test('the Library tab brings the grid back', async ({ page }) => {
  await page.getByTestId('tab-library').click();
  await expect(page.getByTestId('loaded-count')).toBeVisible();
  await expect(page.getByTestId('benchmark-setup')).toBeHidden();
});
