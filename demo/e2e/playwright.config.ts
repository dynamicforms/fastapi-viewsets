import { defineConfig, devices } from '@playwright/test';

const API_PORT = 8123;
const FE_PORT = 15173;

/**
 * End-to-end cover for the demo, which is the only place the whole stack is assembled: the
 * viewsets, both transports, both backends, cursor paging and the grid's incremental loading.
 * Unit tests cover each of those; nothing but this covers them wired together.
 *
 * Its own ports, so a run never depends on - or fights with - a dev server the developer already
 * has up.
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
      // Deliberately the string form. Uvicorn imports the app inside its own event loop when given
      // "module:app", and anything the demo does at import time has to survive that - which is
      // exactly what once broke, invisibly, because passing the app object imports it earlier.
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
