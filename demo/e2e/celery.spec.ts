import { execFileSync } from 'node:child_process';
import { expect, test } from '@playwright/test';

/**
 * The Celery path, which running the demo no longer exercises by itself.
 *
 * It is off by default because it dominates the latency the benchmark exists to measure - but
 * "off by default" quietly became "never run outside unit tests", and the thing about a worker
 * process is that it fails in ways an in-process test cannot reproduce.
 *
 * Skipped rather than failed when Redis is absent: it is the one piece of infrastructure the demo
 * needs that a checkout does not come with.
 */

const API_PORT = 8124;

function redisIsUp(): boolean {
  try {
    execFileSync('python', ['-c', 'import redis; redis.Redis().ping()'], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

test.describe('the Celery-backed demo', () => {
  test.skip(!redisIsUp(), 'needs Redis on localhost:6379');

  let backend: ReturnType<typeof import('node:child_process').spawn>;
  let worker: ReturnType<typeof import('node:child_process').spawn>;

  test.beforeAll(async () => {
    const { spawn } = await import('node:child_process');
    const env = { ...process.env, DEMO_CELERY: '1', DEMO_LIBRARY_SIZE: '200' };
    const cwd = new URL('../..', import.meta.url).pathname;

    worker = spawn('celery', ['-A', 'demo.backend.celery_worker', 'worker', '--loglevel=error',
      '--concurrency=2'], { cwd, env, stdio: 'ignore' });
    backend = spawn('python', ['-m', 'uvicorn', 'demo.backend.main:app', '--host', '127.0.0.1',
      '--port', String(API_PORT)], { cwd, env, stdio: 'ignore' });

    // The worker has to be registered on the queue before the first request, or the API waits for
    // a result nobody will produce.
    const deadline = Date.now() + 90_000;
    for (;;) {
      try {
        const response = await fetch(`http://127.0.0.1:${API_PORT}/music?limit=1`);
        if (response.ok) return;
      } catch {
        /* not up yet */
      }
      if (Date.now() > deadline) throw new Error('the Celery-backed demo never came up');
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  });

  test.afterAll(() => {
    backend?.kill();
    worker?.kill();
  });

  test('serves a cursor page through the worker', async () => {
    const page = await (await fetch(`http://127.0.0.1:${API_PORT}/music?limit=5&sort=id:asc`)).json();
    expect(page.results).toHaveLength(5);
    expect(page.next).toBeTruthy();
  });

  test('the cursor still walks the collection when every call is a Celery task', async () => {
    // The whole list pipeline runs in the worker, so this is the only check that a cursor issued
    // there is still valid when it comes back through the API.
    const seen: number[] = [];
    let cursor: string | null = null;
    for (let page = 0; page < 10; page += 1) {
      const query = new URLSearchParams({ limit: '25', sort: 'year:asc' });
      if (cursor) query.set('cursor', cursor);
      const body = await (await fetch(`http://127.0.0.1:${API_PORT}/music?${query}`)).json();
      seen.push(...body.results.map((record: { id: number }) => record.id));
      cursor = body.next;
      if (!cursor) break;
    }
    expect(seen).toHaveLength(200);
    expect(new Set(seen).size).toBe(200);
  });

  test('filters are translated in the worker too', async () => {
    const body = await (await fetch(`http://127.0.0.1:${API_PORT}/music?limit=50&year__gte=2015`)).json();
    expect(body.results.length).toBeGreaterThan(0);
    for (const record of body.results) expect(record.year).toBeGreaterThanOrEqual(2015);
  });
});
