import type { AnalysisResponse, DistanceRule, Origin } from './api-contract';

export type MockScenario = 'complete' | 'partial' | 'failed' | 'empty';
export type SyntheticRequest = {
  origin: Origin;
  coordinate_system: 'bd09ll';
  scenario?: 'plane' | 'local_failure' | 'global_failure';
  budget?: number;
  distance_rule?: Partial<DistanceRule>;
};

export class AnalysisHttpError extends Error {
  constructor(public readonly statusCode: number, public readonly result: AnalysisResponse) {
    super(result.errors[0]?.message ?? '分析请求失败');
  }
}

// This client uses geographic coordinates. Do not cast its results to the legacy canvas AnalysisResult.
export function createAnalysisClient(baseUrl: string, fetcher: typeof fetch = fetch) {
  async function request(path: string, init?: RequestInit): Promise<AnalysisResponse> {
    const response = await fetcher(`${baseUrl.replace(/\/$/, '')}/api/v1/analysis/${path}`, init);
    const body = await response.json();
    // Version/shape guard; full schema is enforced by FastAPI and shared fixtures.
    if (body?.schema_version !== '1.0' || body?.coordinate_system !== 'bd09ll'
      || !['complete', 'partial', 'failed', 'empty'].includes(body?.status)
      || !body.data || !Array.isArray(body.errors)) {
      throw new Error('接口响应版本或格式不匹配');
    }
    if (!response.ok) throw new AnalysisHttpError(response.status, body);
    return body;
  }
  return {
    mock: (scenario: MockScenario, signal?: AbortSignal) => request(`mock/${scenario}`, { signal }),
    synthetic: (body: SyntheticRequest, signal?: AbortSignal) => request('synthetic', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal,
    }),
  };
}
