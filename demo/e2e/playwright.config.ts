import { defineConfig, devices } from '@playwright/test';

const API_PORT = 8123;
const FE_PORT = 15173;

/**
 * With `DEMO_DATABASE_URL` the backend starts through `demo.py`, which adds the benchmark's latency proxy; with
 * `DEMO_REDIS_URL` as well, also the benchmark's two Celery workers. Without them it is uvicorn alone.
 */
const DATABASE_URL = process.env.DEMO_DATABASE_URL ?? '';
const REDIS_URL = process.env.DEMO_REDIS_URL ?? '';
const backendCommand = DATABASE_URL
  ? `python demo.py --no-frontend${REDIS_URL ? ' --benchmark' : ''}`
  : `python -m uvicorn demo.backend.main:app --host 127.0.0.1 --port ${API_PORT}`;

/**
 * End-to-end tests of the assembled demo stack: both transports, both backends, cursor paging, grid
 * loading and change events across tabs. Uses its own ports, independent of running dev servers.
 */
export default defineConfig({
  testDir: '.',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: 'list',
  // Under demo/, like everything else here: none of this is the library.
  outputDir: '../test-results',
  timeout: 90_000,
  forbidOnly: !!process.env.CI,

  use: {
    baseURL: `http://127.0.0.1:${FE_PORT}`,
    trace: 'on-first-retry',
  },

  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],

  webServer: [
    {
      // "module:app" form: uvicorn then imports the app inside its event loop, as in the demo.
      command: backendCommand,
      url: `http://127.0.0.1:${API_PORT}/music?limit=1`,
      cwd: '../..',
      env: { DEMO_API_PORT: String(API_PORT), DEMO_FE_PORT: String(FE_PORT) },
      reuseExistingServer: false,
      timeout: 120_000,
      // demo.py stops its children (proxy, workers) on SIGTERM; SIGKILL would leave them running
      gracefulShutdown: { signal: 'SIGTERM', timeout: 20_000 },
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command: `npx vite --port ${FE_PORT} --strictPort`,
      url: `http://127.0.0.1:${FE_PORT}`,
      cwd: '../frontend',
      reuseExistingServer: false,
      timeout: 120_000,
      env: { DEMO_API_PORT: String(API_PORT), DEMO_FE_PORT: String(FE_PORT) },
    },
  ],
});
