import { afterEach, describe, expect, it, vi } from 'vitest';
import type {
  BaiduMapApi, BMapGeolocationResult, BMapLocalResultPoi, BMapLocalSearchOptions, BMapLocalSearchResult,
} from '../map/baiduMapTypes';
import { formatAddress, locate, locationNotice, searchPlaces } from './location';

afterEach(() => vi.useRealTimers());

const Point = class { constructor(public lng: number, public lat: number) {} };

function createGeolocationApi() {
  let callback: ((result: BMapGeolocationResult | null) => void) | null = null;
  let status = 0;
  class Geolocation {
    getCurrentPosition(cb: (result: BMapGeolocationResult | null) => void) { callback = cb; }
    getStatus() { return status; }
  }
  return {
    api: { Point, Geolocation } as unknown as BaiduMapApi,
    respond(result: BMapGeolocationResult | null, nextStatus = 0) {
      status = nextStatus;
      callback?.(result);
    },
  };
}

type FakePoi = Partial<BMapLocalResultPoi> & { point?: { lng: number; lat: number } };

function createSearchApi(pois: FakePoi[] = []) {
  let complete: ((results: BMapLocalSearchResult | null) => void) | null = null;
  const calls: { keyword: string; radius: number }[] = [];
  class LocalSearch {
    constructor(public location: unknown, public options: BMapLocalSearchOptions) {}
    searchNearby(keyword: string, center: unknown, radius: number) {
      calls.push({ keyword, radius });
      complete = this.options.onSearchComplete ?? null;
    }
    search() {}
    clearResults() {}
  }
  const result: BMapLocalSearchResult = {
    getPoi: index => pois[index] as BMapLocalResultPoi,
    getCurrentNumPois: () => pois.length,
    getNumPois: () => pois.length,
  };
  return {
    api: { Point, LocalSearch } as unknown as BaiduMapApi,
    calls,
    finish: (value: BMapLocalSearchResult | null = result) => complete?.(value),
  };
}

describe('locate', () => {
  it('returns rounded BD09LL coordinates, accuracy and deduplicated address', async () => {
    const { api, respond } = createGeolocationApi();
    const promise = locate(api);
    respond({
      point: { lng: 116.404123456, lat: 39.915123456 }, accuracy: 35.6,
      address: { province: '北京市', city: '北京市', district: '东城区', street: '东华门大街', streetNumber: '20号' },
    });
    await expect(promise).resolves.toEqual({
      center: { lng: 116.404123, lat: 39.915123 }, accuracy: 36, address: '北京市东城区东华门大街20号',
    });
  });

  it('maps permission denial, timeout and unavailable status codes', async () => {
    const denied = createGeolocationApi();
    const deniedPromise = locate(denied.api);
    denied.respond(null, 6);
    await expect(deniedPromise).rejects.toMatchObject({ code: 'permission' });

    const timedOut = createGeolocationApi();
    const timeoutPromise = locate(timedOut.api);
    timedOut.respond(null, 8);
    await expect(timeoutPromise).rejects.toMatchObject({ code: 'timeout' });

    const unavailable = createGeolocationApi();
    const unavailablePromise = locate(unavailable.api);
    unavailable.respond(null, 2);
    await expect(unavailablePromise).rejects.toMatchObject({ code: 'unavailable' });
  });

  it('rejects invalid coordinates and unavailable SDK', async () => {
    const { api, respond } = createGeolocationApi();
    const promise = locate(api);
    respond({ point: { lng: Number.NaN, lat: 39.915 } });
    await expect(promise).rejects.toMatchObject({ code: 'invalid' });

    await expect(locate({ Point } as unknown as BaiduMapApi)).rejects.toMatchObject({ code: 'unsupported' });
  });

  it('rejects when the SDK never calls back or the constructor throws', async () => {
    vi.useFakeTimers();
    const { api } = createGeolocationApi();
    const promise = locate(api);
    const assertion = expect(promise).rejects.toMatchObject({ code: 'timeout' });
    await vi.advanceTimersByTimeAsync(12_000);
    await assertion;
    vi.useRealTimers();

    class Broken { constructor() { throw new Error('boom'); } }
    await expect(locate({ Point, Geolocation: Broken } as unknown as BaiduMapApi))
      .rejects.toMatchObject({ code: 'failed' });
  });
});

describe('searchPlaces', () => {
  it('normalizes POI results, skips invalid points and passes trimmed keyword and radius', async () => {
    const { api, calls, finish } = createSearchApi([
      { title: '人民公园', address: '测试路1号', uid: 'poi-1', point: { lng: 116.41, lat: 39.92 } },
      { title: ' ', point: { lng: 116.42, lat: 39.93 } },
      { title: '坏点', point: { lng: Number.NaN, lat: 39.94 } },
    ]);
    const promise = searchPlaces(api, '  公园  ', { lng: 116.4, lat: 39.9 });
    finish();
    await expect(promise).resolves.toEqual([
      { id: 'poi-1', title: '人民公园', address: '测试路1号', center: { lng: 116.41, lat: 39.92 } },
      { id: 'index-1', title: '未命名地点', address: null, center: { lng: 116.42, lat: 39.93 } },
    ]);
    expect(calls).toEqual([{ keyword: '公园', radius: 5_000 }]);
  });

  it('honors radius and limit options', async () => {
    const { api, calls, finish } = createSearchApi([
      { title: 'A', point: { lng: 116.41, lat: 39.92 } },
      { title: 'B', point: { lng: 116.42, lat: 39.93 } },
    ]);
    const promise = searchPlaces(api, '公园', { lng: 116.4, lat: 39.9 }, { radius: 800, limit: 1 });
    finish();
    await expect(promise).resolves.toHaveLength(1);
    expect(calls[0].radius).toBe(800);
  });

  it('rejects empty keyword, empty results, missing SDK, null results and timeouts', async () => {
    await expect(searchPlaces({ Point } as unknown as BaiduMapApi, '  ', { lng: 0, lat: 0 }))
      .rejects.toMatchObject({ code: 'invalid' });
    await expect(searchPlaces({ Point } as unknown as BaiduMapApi, '公园', { lng: 0, lat: 0 }))
      .rejects.toMatchObject({ code: 'unsupported' });

    const empty = createSearchApi([]);
    const emptyPromise = searchPlaces(empty.api, '公园', { lng: 0, lat: 0 });
    empty.finish();
    await expect(emptyPromise).rejects.toMatchObject({ code: 'no-results' });

    const nulled = createSearchApi([{ title: 'A', point: { lng: 1, lat: 1 } }]);
    const nullPromise = searchPlaces(nulled.api, '公园', { lng: 0, lat: 0 });
    nulled.finish(null);
    await expect(nullPromise).rejects.toMatchObject({ code: 'no-results' });

    vi.useFakeTimers();
    const { api } = createSearchApi();
    const timeoutPromise = searchPlaces(api, '公园', { lng: 0, lat: 0 });
    const assertion = expect(timeoutPromise).rejects.toMatchObject({ code: 'timeout' });
    await vi.advanceTimersByTimeAsync(12_000);
    await assertion;
    vi.useRealTimers();
  });
});

describe('location helpers', () => {
  it('formats partial addresses and null when nothing is usable', () => {
    expect(formatAddress({ city: '北京市', district: '东城区' })).toBe('北京市东城区');
    expect(formatAddress({ province: '北京市', city: '北京市' })).toBe('北京市');
    expect(formatAddress({})).toBeNull();
    expect(formatAddress(undefined)).toBeNull();
  });

  it('only enables controls in real map mode', () => {
    expect(locationNotice('real')).toBeNull();
    expect(locationNotice('loading')).toContain('正在加载');
    expect(locationNotice('fallback')).toContain('浏览器地图密钥');
  });
});
