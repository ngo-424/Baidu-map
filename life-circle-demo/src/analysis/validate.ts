import type { AnalysisResult, BusinessStatus, TaskStatus } from './types';

type RecordValue = Record<string, unknown>;
const object = (v: unknown): v is RecordValue => v !== null && typeof v === 'object' && !Array.isArray(v);
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const count = (v: unknown) => finite(v) && Number.isInteger(v) && v >= 0;
const source = (v: unknown) => v === 'synthetic' || v === 'baidu_walking';
const businessStatus = (v: unknown): v is BusinessStatus =>
  v === 'complete' || v === 'partial' || v === 'failed' || v === 'empty';
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
    && v.schema_version === '1.0' && v.responseType === 'task'
    && ['running', 'cancelling', 'completed', 'cancelled', 'failed'].includes(v.status as string)
    && (v.businessStatus === null || businessStatus(v.businessStatus))
    && typeof v.stage === 'string' && count(v.requests) && count(v.networkRequests)
    && [200, 400, 800].includes(v.budget as number) && finite(v.elapsedSeconds) && v.elapsedSeconds >= 0
    && source(v.dataSource) && (v.error === null || typeof v.error === 'string');
}

export function validResult(v: unknown): v is AnalysisResult {
  if (!object(v) || typeof v.taskId !== 'string' || !v.taskId || !source(v.dataSource)
    || v.schema_version !== '1.0' || v.responseType !== 'result' || v.taskStatus !== 'completed'
    || !businessStatus(v.status) || !businessStatus(v.businessStatus) || v.status !== v.businessStatus
    || !object(v.center) || !point([v.center.lng, v.center.lat]) || !finite(v.generatedAt)
    || !['not_integrated', 'complete', 'partial', 'failed'].includes(v.facilitiesStatus as string) || v.coordinateSystem !== 'bd09ll'
    || v.coordinateOrder !== 'longitude,latitude' || !object(v.units) || !object(v.rules)
    || !object(v.data) || !object(v.isochrone)) return false;
  const r = v.isochrone;
  if (r.timeBands !== undefined && (!Array.isArray(r.timeBands) || !r.timeBands.every(b => object(b) && [5,10,15].includes(b.minutes as number) && (b.geometry === null || geometry(b.geometry))))) return false;
  if (r.unreachableRegion !== undefined && r.unreachableRegion !== null && !geometry(r.unreachableRegion)) return false;
  if (v.facilityAnalysis !== null && v.facilityAnalysis !== undefined) {
    const a = v.facilityAnalysis;
    if (!object(a) || !['complete','partial','failed'].includes(a.status as string)
      || !count(a.candidate_points) || !count(a.assessed_points) || !count(a.unassessed_points) || !count(a.network_requests)
      || !finite(a.elapsed_seconds) || !Array.isArray(a.warnings) || !a.warnings.every(w=>typeof w==='string')
      || !Array.isArray(a.queries) || !a.queries.every(q=>object(q) && typeof q.query==='string' && ['complete','partial','failed','truncated'].includes(q.status as string))
      || !Array.isArray(a.assessments) || !a.assessments.every(p=>object(p) && object(p.location) && point([p.location.lng,p.location.lat]) && finite(p.duration_s)
        && Array.isArray(p.categories) && p.categories.every(c=>object(c) && ['shopping','medical','education'].includes(c.category as string) && ['covered','blind','unknown'].includes(c.status as string)))
      || !object(a.routes) || !Object.values(a.routes).every(route=>object(route) && Array.isArray(route.path) && route.path.every(point))
      || (a.serviceBlindRegions !== undefined && (!object(a.serviceBlindRegions) || !Object.values(a.serviceBlindRegions).every(g=>geometry(g))))
      || !Array.isArray(v.data.facilities) || !v.data.facilities.every(f=>object(f) && typeof f.id==='string' && typeof f.name==='string' && ['shopping','medical','education'].includes(f.major_category as string)
        && object(f.location) && point([f.location.lng,f.location.lat]) && (f.in_circle === null || typeof f.in_circle==='boolean'))
      || typeof v.data.report !== 'string') return false;
  } else if (v.facilitiesStatus !== 'not_integrated') return false;
  if (r.coordinateSystem !== 'bd09ll' || !(r.geometry === null || geometry(r.geometry))
    || !geometry(r.unknownRegion) || !geometry(r.uncertainRegion) || !geometry(r.computationExtent)
    || !['usable', 'partial', 'insufficient'].includes(r.quality as string)
    || typeof r.stopReason !== 'string' || !Array.isArray(r.warnings) || !r.warnings.every(x => typeof x === 'string')
    || !object(r.statistics) || !object(r.config)) return false;
  const s = r.statistics;
  return count(s.requests) && count(s.network_requests) && count(s.retries) && count(s.unfinished_boundary)
    && finite(s.total_seconds) && s.total_seconds >= 0 && finite(s.unknown_area) && s.unknown_area >= 0
    && object(s.failures) && Object.values(s.failures).every(count)
    && point(r.config.origin) && [200, 400, 800].includes(r.config.budget as number) && count(r.config.seed);
}
