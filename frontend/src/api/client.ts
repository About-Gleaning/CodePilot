export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryAfter: number | null;

  constructor(message: string, status: number, code = 'request_failed', retryAfter: number | null = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

type ErrorBody = {
  detail?: string | { code?: string; message?: string };
};

export async function apiRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.json().catch(() => null) as ErrorBody | null;
    const detail = body?.detail;
    const code = typeof detail === 'object' && detail?.code ? detail.code : 'request_failed';
    const message = typeof detail === 'object' && detail?.message
      ? detail.message
      : typeof detail === 'string'
        ? detail
        : friendlyStatusMessage(response.status);
    const retryAfterRaw = response.headers.get('Retry-After');
    const retryAfter = retryAfterRaw && Number.isFinite(Number(retryAfterRaw)) ? Number(retryAfterRaw) : null;
    throw new ApiError(message, response.status, code, retryAfter);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return response.json() as Promise<T>;
}

export function apiJson<T>(url: string, method: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return apiRequest<T>(url, {
    method,
    signal,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function friendlyStatusMessage(status: number): string {
  if (status === 404) return '目标资源不存在或已不可用。';
  if (status === 409) return '资源状态已变化，请刷新后重试。';
  if (status === 422) return '请求内容不符合要求。';
  return '请求失败，请稍后重试。';
}
