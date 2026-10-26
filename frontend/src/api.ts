export class ApiError extends Error {
  status: number;
  code?: string;
  payload: unknown;

  constructor(message: string, status: number, code: string | undefined, payload: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}

export function apiErrorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401) {
    window.dispatchEvent(new CustomEvent('dmr:unauthorized'));
  }
  if (!response.ok) {
    const payload = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    const message = String(payload.error || payload.detail || payload.message || `请求失败 ${response.status}`);
    const code = typeof payload.code === 'string' ? payload.code : undefined;
    throw new ApiError(message, response.status, code, data);
  }
  return data as T;
}

export const authEventName = 'dmr:unauthorized';
