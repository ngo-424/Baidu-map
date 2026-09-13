import type { AnalysisResult, TaskStatus } from './types';

type RecordValue = Record<string, unknown>;
const object = (v: unknown): v is RecordValue => v !== null && typeof v === 'object' && !Array.isArray(v);
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const count = (v: unknown) => finite(v) && Number.isInteger(v) && v >= 0;
const source = (v: unknown) => v === 'synthetic' || v === 'baidu_walking';
const point = (v: unknown) => Array.isArray(v) && v.length === 2 && finite(v[0]) && finite(v[1])
  && v[0] >= -180 && v[0] <= 180 && v[1] > -85 && v[1] < 85;

function geometry(v: unknown): boolean {
  return object(v) && v.type === 'MultiPolygon' && v.coordinateSystem === 'bd09ll'
    && Array.isArray(v.coordinates) && v.coordinates.every(p => Array.isArray(p) && p.length > 0
      && p.every(r => Array.isArray(r) && r.length >= 4 && r.every(point)
        && r[0][0] === r[r.length - 1][0] && r[0][1] === r[r.length - 1][1]));
}

export function validTask(v: unknown): v is TaskStatus {
  return object(v) && typeof v.taskId === 'string' && v.taskId.length > 0
    && ['running', 'cancelling', 'completed', 'cancelled', 'failed'].includes(v.status as string)
    && typeof v.stage === 'string' && count(v.requests) && count(v.networkRequests)
    && [200, 400, 800].includes(v.budget as number) && finite(v.elapsedSeconds) && v.elapsedSeconds >= 0
    && source(v.dataSource) && (v.error === null || typeof v.error === 'string');
}

export function validResult(v: unknown): v is AnalysisResult {
  if (!object(v) || typeof v.taskId !== 'string' || !v.taskId || !source(v.dataSource)
    || !object(v.center) || !point([v.center.lng, v.center.lat]) || !finite(v.generatedAt)
    || v.generatedAt < 0 || !Number.isFinite(new Date(v.generatedAt * 1000).getTime())
    || v.facilitiesStatus !== 'not_integrated' || !object(v.isochrone)) return false;
  const r = v.isochrone;
  if (r.coordinateSystem !== 'bd09ll' || !(r.geometry === null || geometry(r.geometry))
    || !geometry(r.unknownRegion) || !geometry(r.uncertainRegion) || !geometry(r.computationExtent)
    || !['usable', 'partial', 'insufficient'].includes(r.quality as string)
    || typeof r.stopReason !== 'string' || !Array.isArray(r.warnings) || !r.warnings.every(x => typeof x === 'string')
    || !object(r.statistics) || !object(r.config)) return false;
  const s = r.statistics;
  return count(s.requests) && count(s.network_requests) && count(s.retries) && count(s.unfinished_boundary)
    && finite(s.total_seconds) && s.total_seconds >= 0 && finite(s.unknown_area) && s.unknown_area >= 0
    && object(s.failures) && Object.values(s.failures).every(count)
    && point(r.config.origin) && (r.config.origin as number[])[0] === v.center.lng
    && (r.config.origin as number[])[1] === v.center.lat
    && [200, 400, 800].includes(r.config.budget as number) && count(r.config.seed);
}
