/** The only place that talks to the API. Types mirror api/app/schemas.py. */

export type Status = "queued" | "running" | "failed" | "succeeded" | "dead";
export type Role = "admin" | "viewer";

export interface User {
  username: string;
  role: Role;
}

export interface Run {
  id: number;
  status: Status;
  source: string;
  idempotency_key: string;
  attempts: number;
  max_attempts: number;
  created_at: string;
  run_at: string;
  finished_at: string | null;
  last_error: string | null;
}

export interface RunPage {
  items: Run[];
  next_before: number | null;
}

export interface Summary {
  counts: Record<Status, number>;
  total: number;
}

export interface Attempt {
  attempt_no: number;
  worker_id: string;
  started_at: string;
  finished_at: string | null;
  ok: boolean | null;
  error: string | null;
  duration_ms: number | null;
}

export interface AuditEntry {
  id: number;
  at: string;
  actor: string;
  action: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
}

export interface RunDetail extends Run {
  locked_by: string | null;
  locked_at: string | null;
  event: { event_id: number; payload: Record<string, unknown>; headers: Record<string, unknown>; received_at: string };
  attempts_log: Attempt[];
  audit: AuditEntry[];
}

export interface Source {
  id: number;
  name: string;
  handler: string;
  max_attempts: number;
  active: boolean;
  created_at: string;
}

export interface LoginResult {
  token: string;
  expires_at: string;
  user: User;
}

export const API_URL: string = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://127.0.0.1:8000";

/** status 0 means the request never got an answer (API down, blocked by CORS, offline). */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public retryAfter?: number,
  ) {
    super(message);
  }
}

// The token lives in sessionStorage: it dies with the tab. Storage can throw (private mode, blocked
// site data), so every access is guarded and the app still works for the current page load.
let memoryToken: string | null = null;
const KEY = "hub.token";

export function getToken(): string | null {
  if (memoryToken) return memoryToken;
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  memoryToken = token;
  try {
    if (token) sessionStorage.setItem(KEY, token);
    else sessionStorage.removeItem(KEY);
  } catch {
    /* in-memory copy still works */
  }
}

let onUnauthorized: () => void = () => {};
/** Called when a request that carried a token is refused, so the UI can drop back to the login page. */
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (init.body) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(API_URL + path, { ...init, headers });
  } catch {
    throw new ApiError(0, `Can't reach the API at ${API_URL}.`);
  }

  if (response.status === 204) return undefined as T;
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    /* non-JSON error body */
  }
  if (!response.ok) {
    if (response.status === 401 && token) onUnauthorized();
    const detail = (body as { detail?: unknown } | null)?.detail;
    const retryAfter = Number(response.headers.get("Retry-After")) || undefined;
    throw new ApiError(response.status, typeof detail === "string" ? detail : `Request failed (${response.status}).`, retryAfter);
  }
  return body as T;
}

export interface RunQuery {
  status?: Status[];
  source?: string;
  since?: string;
  before?: number;
  limit?: number;
}

function queryString(params: Record<string, string | number | string[] | undefined>): string {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    for (const v of Array.isArray(value) ? value : [value]) q.append(key, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

export const api = {
  login: (username: string, password: string) =>
    request<LoginResult>("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  me: () => request<User>("/auth/me"),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  runs: (q: RunQuery) => request<RunPage>("/runs" + queryString({ ...q })),
  summary: (since?: string) => request<Summary>("/runs/summary" + queryString({ since })),
  run: (id: number) => request<RunDetail>(`/runs/${id}`),
  retry: (id: number) => request<RunDetail>(`/runs/${id}/retry`, { method: "POST" }),
  sources: () => request<Source[]>("/sources"),
};
