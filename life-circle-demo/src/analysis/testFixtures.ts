import type { AnalysisResult, BusinessGeometry } from './types';

/** Offline contract fixture only; never used by the application. */
export function resultFixture(): AnalysisResult {
  const empty: BusinessGeometry = { type: 'MultiPolygon', coordinateSystem: 'bd09ll', coordinates: [] };
  return {
    taskId: 'one', center: { lng: 116.404, lat: 39.915 }, dataSource: 'synthetic',
    generatedAt: 1_789_200_000, facilitiesStatus: 'not_integrated',
    isochrone: {
      coordinateSystem: 'bd09ll', geometry: { ...empty, coordinates: [
        [[[116.4, 39.9], [116.5, 39.9], [116.5, 40], [116.4, 39.9]],
          [[116.42, 39.92], [116.44, 39.92], [116.44, 39.94], [116.42, 39.92]]],
        [[[116.6, 39.9], [116.7, 39.9], [116.7, 40], [116.6, 39.9]]],
      ] },
      unknownRegion: empty, uncertainRegion: empty, computationExtent: empty,
      quality: 'usable', stopReason: 'budget', warnings: [],
      statistics: { requests: 200, network_requests: 0, retries: 0, unknown_area: 0,
        unfinished_boundary: 0, total_seconds: 1, failures: {} },
      config: { origin: [116.404, 39.915], budget: 200, seed: 20260911,
        threshold: 900, coordinate_system: 'bd09ll' },
    },
  };
}
