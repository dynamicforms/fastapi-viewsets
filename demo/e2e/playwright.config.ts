import { defineConfig, devices } from '@playwright/test';

const API_PORT = 8123;
const FE_PORT = 15173;

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
      command: `python -m uvicorn demo.backend.main:app --host 127.0.0.1 --port ${API_PORT}`,
      url: `http://127.0.0.1:${API_PORT}/music?limit=1`,
      cwd: '../..',
      reuseExistingServer: false,
      timeout: 120_000,
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
