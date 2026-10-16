import { JsonCodec, memoryPair, Peer, ResetCode, type Stream } from 'muxws';
import { expect, it } from 'vitest';

import { attachEvents, createEventClient, defineEvent } from './events/client';
import { MuxwsProxyImpl } from './muxws-proxy';

interface Item {
  id: number;
}

class Items extends MuxwsProxyImpl<number, Item, 'id'> {
  constructor(peer: Peer) {
    super({
      peer,
      basePath: '/items',
      pkFieldName: 'id',
      validateSchema: false,
      repeat: { withinMs: 5000, delayMs: 10 },
    });
  }
}

it('repeats a refused stream with the headers an open interceptor added to the first one', async () => {
  const [left, right] = memoryPair();
  const client = new Peer(left, { codec: new JsonCodec(), isDialer: true });
  const server = new Peer(right, { codec: new JsonCodec(), isDialer: false });
  const seen: Record<string, unknown>[] = [];
  server.onStream(async (_payload: unknown, stream: Stream) => {
    seen.push({ ...stream.headers });
    if (seen.length === 1) {
      await stream.reset(ResetCode.REFUSED);
      return undefined;
    }
    await stream.reply({ id: 1 });
    return undefined;
  });
  void client.serve().catch(() => undefined);
  void server.serve().catch(() => undefined);

  let bound: string | null = 'T1';
  client.interceptOpen((headers) => {
    if (bound !== null) headers['x-token'] = bound;
  });

  const call = new Items(client).retrieve(1);
  bound = null;
  expect(await call).toEqual({ id: 1 });

  expect(seen).toHaveLength(2);
  expect(seen[0]['x-token']).toBe('T1');
  expect(seen[1]['x-token']).toBe('T1');
  expect(seen[1]['Idempotency-Key']).toBe(seen[0]['Idempotency-Key']);
  expect(seen[1]['Idempotency-Key']).toBe(`"${call.key}"`);
  expect(seen[1][':path']).toBe('/items/1');
});

it('subscribes a topic and receives a push over a real muxws pair', async () => {
  const [left, right] = memoryPair();
  const client = new Peer(left, { codec: new JsonCodec(), isDialer: true });
  const server = new Peer(right, { codec: new JsonCodec(), isDialer: false });
  const seen: { headers: Record<string, unknown>; payload: unknown }[] = [];
  server.onStream(async (payload: unknown, stream: Stream) => {
    seen.push({ headers: { ...stream.headers }, payload });
    if (stream.headers[':path'] === '/events/subscribe') {
      await stream.reply({ rev: 5 }, { headers: { ':status': 200 } });
    } else {
      await stream.reply(null, { headers: { ':status': 204 } });
    }
    return undefined;
  });
  void client.serve().catch(() => undefined);
  void server.serve().catch(() => undefined);

  attachEvents(client);
  const events = createEventClient({ basePath: '/events', peer: client });
  const ImportFinished = defineEvent<{ file: string; rows: number }>('import_finished');
  const received: [{ file: string; rows: number }, number][] = [];
  const subscription = events.subscribe('imports:7', ImportFinished, {
    onEvent: (payload, meta) => received.push([payload, meta.version]),
    onResync: () => {},
  });
  await subscription.ready;

  await server.notify(
    { file: 'members.csv', rows: 412 },
    { headers: { ':event': 'import_finished', ':version': 2, ':topic': 'imports:7', ':rev': 6, ':prev': 5 } },
  );
  await expect.poll(() => received).toEqual([[{ file: 'members.csv', rows: 412 }, 2]]);

  subscription.unsubscribe();
  await expect.poll(() => seen.length).toBe(2);
  events.close();

  expect(seen.map(({ headers, payload }) => [headers[':method'], headers[':path'], payload])).toEqual([
    ['POST', '/events/subscribe', { topic: 'imports:7' }],
    ['POST', '/events/unsubscribe', { topic: 'imports:7' }],
  ]);
  for (const { headers } of seen) expect(headers).not.toHaveProperty('Idempotency-Key');
});
