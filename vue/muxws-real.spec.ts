import { JsonCodec, memoryPair, Peer, ResetCode, type Stream } from 'muxws';
import { expect, it } from 'vitest';

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
