import { describe, expect, it } from 'vitest';
import { analysisAvailability, analysisReportView, decodeAnalysisResult, matchesAnalysisInput } from './adapter';
import { resultFixture } from './testFixtures';

describe('task response adaptation and report boundary', () => {
  it('copies geographic results without losing holes, components, or mutating the response', () => {
    const wire = resultFixture();
    const result = decodeAnalysisResult(wire);
    expect(result).toEqual(wire);
    result.isochrone.geometry!.coordinates[0][1][0][0] = 100;
    expect(wire.isochrone.geometry!.coordinates[0][1][0][0]).toBe(116.42);
  });
  it('keeps null, empty, and partial evidence distinct; no whole-checkup success is invented', () => {
    const result = resultFixture();
    expect(analysisAvailability(result)).toBe('partial');
    result.isochrone.quality = 'partial';
    expect(analysisAvailability(result)).toBe('partial');
    result.isochrone.geometry!.coordinates = [];
    expect(analysisReportView(result).geometrySummary).toContain('为空');
    expect(analysisAvailability(result)).toBe('partial');
    result.isochrone.geometry = null;
    expect(analysisAvailability(result)).toBe('unavailable');
    expect(analysisReportView(result).geometrySummary).toContain('无法确定');
  });
  it('reports provenance and seconds timestamp, with missing facilities and blind zones as null', () => {
    const result = resultFixture();
    const view = analysisReportView(result);
    expect(view.generatedAt).toBe(new Date(result.generatedAt * 1000).toISOString());
    expect(view.dataSource).toContain('合成');
    expect(view.facilityStats.map(x => x.count)).toEqual([null, null, null]);
    expect(view.blindZoneCount).toBeNull();
    result.dataSource = 'baidu_walking';
    expect(analysisReportView(result).dataSource).toBe('百度步行路线数据');
  });
  it('rejects invalid timestamps, inconsistent centers and malformed geographic rings', () => {
    for (const change of [
      (r: ReturnType<typeof resultFixture>) => { r.generatedAt = 1e30; },
      (r: ReturnType<typeof resultFixture>) => { r.center.lng = 120; },
      (r: ReturnType<typeof resultFixture>) => { r.isochrone.geometry!.coordinates[0][0].pop(); },
    ]) {
      const result = resultFixture(); change(result);
      expect(() => decodeAnalysisResult(result)).toThrow('格式异常');
    }
  });
  it('matches normalized request coordinates and the actual analysis budget', () => {
    const result = resultFixture();
    const input = { center: { lng: 116.4040001, lat: 39.915 }, budget: 200 as const, clientRequestId: 'test' };
    expect(matchesAnalysisInput(result, input)).toBe(true);
    expect(matchesAnalysisInput(result, { ...input, budget: 400 })).toBe(false);
    expect(matchesAnalysisInput(result, { ...input, center: { lng: 120, lat: 39 } })).toBe(false);
  });
});
