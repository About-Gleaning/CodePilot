export type ApiIssue = { code: string; field?: string | null; message: string; suggestion?: string;
  resource_kind?: string; resource_id?: string; resource_name?: string | null;
  referenced_by?: Array<{ agent_id: string; name: string }>; action?: 'publish' | 'restore' | null };
export type PublicationProgress = { resource_id: string; resource_name: string; action: 'publish' | 'restore' | 'publish_agent'; status: 'pending' | 'completed' | 'failed' };

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryAfter: number | null;
  readonly issues: ApiIssue[];
  readonly progress: PublicationProgress[];
  readonly retryable: boolean;

  constructor(message: string, status: number, code = 'request_failed', retryAfter: number | null = null, issues: ApiIssue[] = [], progress: PublicationProgress[] = [], retryable = false) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
    this.issues = issues;
    this.progress = progress;
    this.retryable = retryable;
  }
}

type ErrorBody = {
  detail?: string | { code?: string; message?: string; issues?: ApiIssue[]; progress?: PublicationProgress[]; retryable?: boolean };
};

export const AUTH_EXPIRED_EVENT = 'codepilot:auth-expired';

export async function apiRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const method = (init?.method || 'GET').toUpperCase();
  const headers = new Headers(init?.headers);
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrf = readCookie('__Host-codepilot_csrf') || readCookie('codepilot_dev_csrf');
    if (csrf) headers.set('X-CodePilot-CSRF', csrf);
  }
  const response = await fetch(url, { ...init, headers, credentials: 'same-origin' });
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
    if (response.status === 401) window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
    const issues = typeof detail === 'object' && Array.isArray(detail?.issues)
      ? detail.issues.filter((item) => item && typeof item.code === 'string' && typeof item.message === 'string') : [];
    const progress = typeof detail === 'object' && Array.isArray(detail?.progress) ? detail.progress : [];
    throw new ApiError(message, response.status, code, retryAfter, issues, progress, typeof detail === 'object' && detail?.retryable === true);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return response.json() as Promise<T>;
}

function readCookie(name: string): string {
  const prefix = `${name}=`;
  const item = document.cookie.split(';').map((value) => value.trim()).find((value) => value.startsWith(prefix));
  return item ? decodeURIComponent(item.slice(prefix.length)) : '';
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
