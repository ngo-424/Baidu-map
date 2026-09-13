import { expect, it, vi } from 'vitest';
import { createApiService } from './service';
import { resultFixture } from './testFixtures';

const task = { taskId: 'one', status: 'completed', stage: 'completed', requests: 200,
  networkRequests: 0, budget: 200, elapsedSeconds: 1, dataSource: 'synthetic', error: null };

it('adapts valid HTTP results and strips unknown top-level fields', async () => {
  const body = { ...resultFixture(), extra: 'not part of the frontend contract' };
  const api = createApiService('', async () => new Response(JSON.stringify(body)));
  expect(await api.result('one')).toEqual(resultFixture());
});

it('uses same-origin API paths when no base URL is configured', async () => {
  vi.stubEnv('VITE_API_BASE_URL', '');
  try {
    const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify(task)));
    await createApiService(undefined, fetcher).status('one');
    expect(fetcher.mock.calls[0][0]).toBe('/api/analyses/one');
  } finally { vi.unstubAllEnvs(); }
});

it('sends BD09 requests and propagates an abort signal for polling', async () => {
  const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify(task), { status: 200 }));
  const api = createApiService('http://127.0.0.1:8000/', fetcher);
  await api.create({ center: { lng: 116.4, lat: 39.9 }, budget: 400, clientRequestId: 'test' });
  expect(fetcher.mock.calls[0][0]).toBe('http://127.0.0.1:8000/api/analyses');
  expect(JSON.parse(fetcher.mock.calls[0][1]!.body as string).coordinateSystem).toBe('bd09ll');
  const controller = new AbortController();
  await api.status('one', controller.signal);
  expect(fetcher.mock.calls[1][0]).toContain('/api/analyses/one');
});

it('rejects incompatible successful JSON before rendering it', async () => {
  const api = createApiService('', async () => new Response('{}', { status: 200 }));
  await expect(api.result('one')).rejects.toThrow('服务版本');
  await expect(api.status('one')).rejects.toThrow('服务版本');
});

it('rejects a response belonging to a different task', async () => {
  const api = createApiService('', async () => new Response(JSON.stringify(task), { status: 200 }));
  await expect(api.status('another')).rejects.toThrow('服务版本');
});

it('cancels by idempotency key without creating a replacement task', async () => {
  const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify(task), { status: 202 }));
  await createApiService('', fetcher).cancelByRequest('original-key');
  expect(fetcher.mock.calls[0][0]).toBe('/api/analyses/by-request/original-key/cancel');
});

it('does not expose raw server or transport errors', async () => {
  const api = createApiService('', async () => new Response('sensitive upstream text', { status: 503 }));
  await expect(api.create({ center: { lng: 0, lat: 0 }, budget: 400, clientRequestId: 'test' })).rejects.toThrow('后端');
});
