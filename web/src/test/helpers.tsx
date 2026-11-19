import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";
import type { Run, RunDetail, Status } from "../api";
import { setToken } from "../api";
import App from "../App";
import { AuthProvider } from "../auth";

export const NOW = "2026-10-29T06:00:00Z"; // 14:00 in Manila

export function run(overrides: Partial<Run> = {}): Run {
  return {
    id: 1,
    status: "succeeded",
    source: "shop",
    idempotency_key: "order-1001",
    attempts: 1,
    max_attempts: 5,
    created_at: NOW,
    run_at: NOW,
    finished_at: NOW,
    last_error: null,
    ...overrides,
  };
}

export function detail(overrides: Partial<RunDetail> = {}): RunDetail {
  return {
    ...run(),
    locked_by: null,
    locked_at: null,
    event: { event_id: 1, payload: { order: 1001 }, headers: {}, received_at: NOW },
    attempts_log: [],
    audit: [],
    ...overrides,
  } as RunDetail;
}

export type Handler = (url: URL, init: RequestInit) => { status?: number; body?: unknown; headers?: Record<string, string> } | Promise<never>;

/** Replace fetch with a table of "METHOD /path" handlers; anything unlisted fails loudly. */
export function mockApi(routes: Record<string, Handler | unknown>) {
  const calls: { method: string; url: URL }[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input));
    const method = (init.method ?? "GET").toUpperCase();
    calls.push({ method, url });
    const entry = routes[`${method} ${url.pathname}`];
    if (entry === undefined) throw new Error(`unmocked request: ${method} ${url.pathname}${url.search}`);
    const result = typeof entry === "function" ? await (entry as Handler)(url, init) : { body: entry };
    const status = (result as { status?: number }).status ?? 200;
    const body = (result as { body?: unknown }).body;
    return new Response(status === 204 ? null : JSON.stringify(body ?? null), {
      status,
      headers: { "Content-Type": "application/json", ...((result as { headers?: Record<string, string> }).headers ?? {}) },
    });
  });
  vi.stubGlobal("fetch", fn);
  return { calls, fn };
}

export const emptySummary = { counts: { queued: 0, running: 0, failed: 0, succeeded: 0, dead: 0 }, total: 0 };

export function summaryOf(counts: Partial<Record<Status, number>>) {
  const full = { ...emptySummary.counts, ...counts };
  return { counts: full, total: Object.values(full).reduce((a, b) => a + b, 0) };
}

export function signedInAs(role: "admin" | "viewer") {
  setToken("test-token");
  return { "GET /auth/me": { username: role === "admin" ? "boss" : "watcher", role } };
}

export function renderApp(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}
