import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { expect, test } from '@playwright/test';
import axios from 'axios';
import { connect, type Peer } from 'muxws';

import { RECORDS_CHANGED, type RecordsChanged } from '../../vue/events';
import { createEventClient, handleEventStream } from '../../vue/events';

/**
 * The demo's Celery path end to end; skipped without Redis on localhost.
 *
 * The worker uses the default prefork pool, so consecutive calls may hit different processes. Backend and
 * worker use their own SQLite file, so the 200-track seed does not replace the main suite's library.
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
  /** Everything the worker logged; a task it ran is logged as `Task <name>[<id>] succeeded`. */
  let workerLog = '';
  let dbDir = '';

  test.beforeAll(async () => {
    const { spawn } = await import('node:child_process');
    dbDir = mkdtempSync(join(tmpdir(), 'demo-celery-'));
    const env = {
      ...process.env,
      DEMO_CELERY: '1',
      DEMO_LIBRARY_SIZE: '200',
      DEMO_DB_PATH: join(dbDir, 'demo.sqlite3'),
    };
    const cwd = new URL('../..', import.meta.url).pathname;

    worker = spawn('celery', ['-A', 'demo.backend.celery_worker', 'worker', '--loglevel=info'], {
      cwd,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    for (const output of [worker.stdout, worker.stderr]) {
      output?.on('data', (chunk: Buffer) => {
        workerLog += chunk.toString();
      });
    }
    backend = spawn('python', ['-m', 'uvicorn', 'demo.backend.main:app', '--host', '127.0.0.1',
      '--port', String(API_PORT)], { cwd, env, stdio: 'ignore' });

    // Wait until the worker serves a request; before that the API waits for a result nobody produces.
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
    if (dbDir) rmSync(dbDir, { recursive: true, force: true });
  });

  test('serves a cursor page through the worker', async () => {
    const page = await (await fetch(`http://127.0.0.1:${API_PORT}/music?limit=5&sort=id:asc`)).json();
    expect(page.results).toHaveLength(5);
    expect(page.next).toBeTruthy();
  });

  test('the cursor still walks the collection when every call is a Celery task', async () => {
    // Cursors are issued by the worker and must stay valid when they come back through the API.
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

  test('a change a worker makes in SQLite is pushed over muxws by the web process and read back', async () => {
    // The worker publishes through Redis and the web process pushes to subscribed sockets. /music-db rows live
    // in the shared SQLite file, so the read-back sees the write in whichever worker process serves it.
    const peer: Peer = await connect(`ws://127.0.0.1:${API_PORT}/ws`);
    const pushed: unknown[] = [];
    peer.onStream((payload, stream) => {
      if (handleEventStream(peer, payload, stream)) pushed.push(stream.headers[':event']);
    });
    const events = createEventClient({ basePath: '/events', peer, axios });
    let received: (change: RecordsChanged<unknown>) => void = () => undefined;
    const change = new Promise<RecordsChanged<unknown>>((resolve) => {
      received = resolve;
    });
    const subscription = events.subscribe('/music-db', RECORDS_CHANGED, { onEvent: (payload) => received(payload) });
    try {
      await subscription.ready;

      const patch = (favorite: boolean) =>
        fetch(`http://127.0.0.1:${API_PORT}/music-db/1`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ favorite }),
        });
      const before = (await (await fetch(`http://127.0.0.1:${API_PORT}/music-db/1`)).json()).favorite;
      expect((await patch(!before)).ok).toBe(true);

      expect(await change).toEqual({ op: 'updated', changes: [{ pk: 1 }] });
      expect(pushed).toContain('records_changed');
      const logBeforeReadBack = workerLog.length;
      expect((await (await fetch(`http://127.0.0.1:${API_PORT}/music-db/1`)).json()).favorite).toBe(!before);
      // The worker log proves the write and the read-back ran there.
      expect(workerLog).toMatch(/Task music_db\.partial_update\[[^\]]+\] succeeded/);
      await expect
        .poll(() => workerLog.slice(logBeforeReadBack))
        .toMatch(/Task music_db\.retrieve\[[^\]]+\] succeeded/);

      expect((await patch(before)).ok).toBe(true);
    } finally {
      subscription.unsubscribe();
      await peer.close();
    }
  });

  test('filters are translated in the worker too', async () => {
    const body = await (await fetch(`http://127.0.0.1:${API_PORT}/music?limit=50&year__gte=2015`)).json();
    expect(body.results.length).toBeGreaterThan(0);
    for (const record of body.results) expect(record.year).toBeGreaterThanOrEqual(2015);
  });
});
