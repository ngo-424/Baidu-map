import type { AnalysisService } from './types';
import { validTask } from './validate';
import { decodeAnalysisResult } from './adapter';

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export function createApiService(base = import.meta.env.VITE_API_BASE_URL?.trim() || '', fetcher: typeof fetch = fetch): AnalysisService {
  async function request<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
    const timeout = AbortSignal.timeout(10_000);
    let response: Response;
    try {
      response = await fetcher(`${base.replace(/\/$/, '')}/api/analyses${path}`, {
        method, headers: { 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
      });
    } catch {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
      throw new Error('无法连接后端或请求超时，请检查服务后重试');
    }
    if (!response.ok) {
      const messages: Record<number, string> = { 404: '任务不存在或已过期，请重新分析',
        409: '任务状态冲突，服务可能仍在运行其他分析，请稍后重试',
        422: '中心坐标或预算不符合要求', 503: '后端步行服务未就绪，请检查 AK 和 QPS 配置' };
      throw new ApiError(messages[response.status] || '后端请求失败，请稍后重试', response.status);
    }
    try {
      const body: unknown = await response.json();
      const value = path.endsWith('/result') ? decodeAnalysisResult(body) : body;
      if (!path.endsWith('/result') && !validTask(value)) throw new Error('Invalid response structure');
      if (method === 'GET' && value && typeof value === 'object' && 'taskId' in value
        && value.taskId !== decodeURIComponent(path.split('/')[1])) throw new Error('Mismatched task');
      return value as T;
    }
    catch { throw new Error('后端返回格式异常，请检查服务版本'); }
  }
  return {
    create: input => request('', 'POST', { ...input, coordinateSystem: 'bd09ll' }),
    status: (id, signal) => request(`/${encodeURIComponent(id)}`, 'GET', undefined, signal),
    result: (id, signal) => request(`/${encodeURIComponent(id)}/result`, 'GET', undefined, signal),
    cancel: id => request(`/${encodeURIComponent(id)}/cancel`, 'POST'),
    cancelByRequest: key => request(`/by-request/${encodeURIComponent(key)}/cancel`, 'POST'),
  };
}
