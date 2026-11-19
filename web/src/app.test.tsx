import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getToken } from "./api";
import { AuthProvider } from "./auth";
import { ErrorBoundary, Layout } from "./components";
import { detail, emptySummary, mockApi, renderApp, run, signedInAs, summaryOf } from "./test/helpers";

beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const listRoutes = (items = [run()], extra: Record<string, unknown> = {}) => ({
  "GET /runs": { items, next_before: null },
  "GET /runs/summary": summaryOf({ succeeded: items.length }),
  "GET /sources": [{ id: 1, name: "shop", handler: "log", max_attempts: 5, active: true, created_at: "2026-10-29T06:00:00Z" }],
  ...extra,
});

describe("signing in", () => {
  it("sends people without a session to the login page", async () => {
    mockApi({});
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Automation Hub" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("signs in and lands on the runs list", async () => {
    const api = mockApi({
      "POST /auth/login": { token: "abc", expires_at: "2026-10-30T00:00:00Z", user: { username: "boss", role: "admin" } },
      ...listRoutes(),
    });
    renderApp("/");
    await userEvent.type(await screen.findByLabelText("Username"), "boss");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("link", { name: "Open run 1" })).toBeInTheDocument();
    expect(getToken()).toBe("abc");
    const login = api.calls.find((c) => c.url.pathname === "/auth/login")!;
    expect(login.method).toBe("POST");
  });

  it("explains a wrong password and clears the field", async () => {
    mockApi({ "POST /auth/login": () => ({ status: 401, body: { detail: "invalid username or password" } }) });
    renderApp("/login");
    await userEvent.type(await screen.findByLabelText("Username"), "boss");
    await userEvent.type(screen.getByLabelText("Password"), "wrong wrong wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That username and password don't match.");
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });

  it("says how long to wait when locked out", async () => {
    mockApi({ "POST /auth/login": () => ({ status: 429, body: { detail: "too many" }, headers: { "Retry-After": "600" } }) });
    renderApp("/login");
    await userEvent.type(await screen.findByLabelText("Username"), "boss");
    await userEvent.type(screen.getByLabelText("Password"), "whatever it is");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in 10 minutes.");
  });

  it("says so when the API can't be reached", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    renderApp("/login");
    await userEvent.type(await screen.findByLabelText("Username"), "boss");
    await userEvent.type(screen.getByLabelText("Password"), "whatever it is");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Can't reach the API/);
  });

  it("drops back to login when the session is rejected mid-use", async () => {
    mockApi({ ...signedInAs("admin"), "GET /runs": () => ({ status: 401, body: { detail: "not authenticated" } }), "GET /runs/summary": () => ({ status: 401, body: {} }), "GET /sources": () => ({ status: 401, body: {} }) });
    renderApp("/");
    expect(await screen.findByLabelText("Username")).toBeInTheDocument();
    expect(getToken()).toBeNull();
  });

  it("signs out", async () => {
    mockApi({ ...signedInAs("admin"), ...listRoutes(), "POST /auth/logout": { status: 204 } });
    renderApp("/");
    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByLabelText("Username")).toBeInTheDocument();
    expect(getToken()).toBeNull();
  });
});

describe("the runs board", () => {
  it("shows runs in plain language with Philippine times", async () => {
    // The fixture times are on 2026-10-29. Freeze the clock to that day so "today" formatting is stable.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-29T08:00:00Z"));
    mockApi({
      ...signedInAs("viewer"),
      ...listRoutes([
        run({ id: 7, status: "dead", attempts: 5, max_attempts: 5, source: "billing", idempotency_key: "inv-77" }),
        run({ id: 6, status: "failed", attempts: 2, run_at: new Date(Date.now() + 5 * 60_000).toISOString() }),
        run({ id: 5 }),
      ]),
    });
    renderApp("/");
    const rows = await screen.findAllByRole("row");
    const body = rows.slice(1);
    expect(within(body[0]).getByText("Gave up")).toBeInTheDocument();
    expect(within(body[0]).getByText("billing")).toBeInTheDocument();
    expect(within(body[0]).getByText("inv-77")).toBeInTheDocument();
    expect(within(body[0]).getByText("14:00")).toBeInTheDocument();
    expect(within(body[1]).getByText("Retrying")).toBeInTheDocument();
    expect(within(body[1]).getByText(/retry in/)).toBeInTheDocument();
    expect(within(body[2]).getByText("Done")).toBeInTheDocument();
    expect(screen.getByText(/Philippine time/)).toBeInTheDocument();
  });

  it("shows the last error under a failed run, and nothing under a run that has no error", async () => {
    const full = "RuntimeError: ClickUp 404: List not found. To do: pick the new list id.";
    mockApi({
      ...signedInAs("viewer"),
      ...listRoutes([
        run({ id: 2, status: "failed", last_error: full }),
        run({ id: 1, status: "queued", last_error: null }),
      ]),
    });
    renderApp("/");
    const shown = await screen.findByText("ClickUp 404: List not found. To do: pick the new list id.");
    expect(shown).toHaveAttribute("title", full);
    expect(document.querySelectorAll(".c-source .err")).toHaveLength(1);
  });

  it("refreshes the board by itself every 5 seconds", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const { calls } = mockApi({ ...signedInAs("viewer"), ...listRoutes() });
    renderApp("/");
    await screen.findByRole("link", { name: "Open run 1" });
    const runLoads = () => calls.filter((c) => c.method === "GET" && c.url.pathname === "/runs").length;
    expect(runLoads()).toBe(1);
    await vi.advanceTimersByTimeAsync(4_900);
    expect(runLoads()).toBe(1);
    await vi.advanceTimersByTimeAsync(200);
    await waitFor(() => expect(runLoads()).toBe(2));
  });

  it("puts the attention count on its own filter", async () => {
    mockApi({ ...signedInAs("viewer"), ...listRoutes(), "GET /runs/summary": summaryOf({ succeeded: 10, failed: 2, dead: 1 }) });
    renderApp("/");
    const tab = await screen.findByRole("button", { name: /Needs attention/ });
    await waitFor(() => expect(tab).toHaveTextContent("3"));
    expect(tab).toHaveClass("alert");
  });

  it("filtering to what needs attention asks the API for failed and dead runs", async () => {
    const api = mockApi({ ...signedInAs("viewer"), ...listRoutes() });
    renderApp("/");
    await userEvent.click(await screen.findByRole("button", { name: /Needs attention/ }));
    await waitFor(() => {
      const last = api.calls.filter((c) => c.url.pathname === "/runs").at(-1)!;
      expect(last.url.searchParams.getAll("status")).toEqual(["failed", "dead"]);
    });
    expect(screen.getByRole("button", { name: /Needs attention/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("the source and time filters reach the API", async () => {
    const api = mockApi({ ...signedInAs("viewer"), ...listRoutes() });
    renderApp("/");
    await screen.findByRole("option", { name: "shop" });
    await userEvent.selectOptions(screen.getByLabelText("Source"), "shop");
    await userEvent.selectOptions(screen.getByLabelText("Received"), "24h");
    await waitFor(() => {
      const last = api.calls.filter((c) => c.url.pathname === "/runs").at(-1)!;
      expect(last.url.searchParams.get("source")).toBe("shop");
      expect(last.url.searchParams.get("since")).toMatch(/^2\d{3}-/);
    });
  });

  it("pages to older runs with the cursor, and can return to the newest", async () => {
    const api = mockApi({
      ...signedInAs("viewer"),
      ...listRoutes(),
      "GET /runs": (url: URL) =>
        url.searchParams.get("before") ? { body: { items: [run({ id: 3 })], next_before: null } } : { body: { items: [run({ id: 9 })], next_before: 9 } },
    });
    renderApp("/");
    await userEvent.click(await screen.findByRole("button", { name: "Older runs" }));
    expect(await screen.findByRole("link", { name: "Open run 3" })).toBeInTheDocument();
    expect(api.calls.some((c) => c.url.searchParams.get("before") === "9")).toBe(true);
    expect(screen.getByText("End of the list")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Back to newest" }));
    expect(await screen.findByRole("link", { name: "Open run 9" })).toBeInTheDocument();
  });

  it("tells a new install how to get its first run", async () => {
    mockApi({ ...signedInAs("viewer"), ...listRoutes([]), "GET /runs/summary": emptySummary });
    renderApp("/");
    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
    expect(screen.getByText(/Send a signed webhook/)).toBeInTheDocument();
  });

  it("offers a way out when filters hide everything", async () => {
    mockApi({ ...signedInAs("viewer"), ...listRoutes([]), "GET /runs/summary": emptySummary });
    renderApp("/?status=dead");
    expect(await screen.findByText("No runs match these filters")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
  });

  it("explains an unreachable API and can try again", async () => {
    let down = true;
    mockApi({
      ...signedInAs("viewer"),
      "GET /sources": [],
      "GET /runs/summary": emptySummary,
      "GET /runs": () => {
        if (down) throw new TypeError("Failed to fetch");
        return { body: { items: [run()], next_before: null } };
      },
    });
    renderApp("/");
    expect(await screen.findByText("The API isn't answering")).toBeInTheDocument();
    down = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("link", { name: "Open run 1" })).toBeInTheDocument();
  });
});

describe("run detail", () => {
  const dead = detail({
    id: 12,
    status: "dead",
    attempts: 2,
    max_attempts: 2,
    last_error: "RuntimeError: card declined",
    attempts_log: [
      { attempt_no: 1, worker_id: "w1", started_at: "2026-10-29T06:00:00Z", finished_at: "2026-10-29T06:00:01Z", ok: false, error: "RuntimeError: card declined", duration_ms: 120 },
      { attempt_no: 2, worker_id: "w1", started_at: "2026-10-29T06:01:00Z", finished_at: "2026-10-29T06:01:01Z", ok: false, error: "RuntimeError: card declined", duration_ms: 98 },
    ],
    audit: [{ id: 1, at: "2026-10-29T06:00:00Z", actor: "w1", action: "job.started", before: null, after: null }],
  });

  it("shows why it gave up, each try in order, the payload and the history", async () => {
    mockApi({ ...signedInAs("viewer"), "GET /runs/12": dead });
    renderApp("/runs/12");
    expect(await screen.findByRole("heading", { name: /Run 12/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Why it gave up" })).toBeInTheDocument();
    const tries = screen.getAllByRole("listitem");
    expect(tries).toHaveLength(2);
    expect(within(tries[0]).getByText("1")).toBeInTheDocument();
    expect(within(tries[1]).getByText("2")).toBeInTheDocument();
    expect(screen.getByText(/"order": 1001/)).toBeInTheDocument();
    expect(screen.getByText("job.started")).toBeInTheDocument();
  });

  it("lets an admin retry a run that gave up, and says what happened", async () => {
    const api = mockApi({ ...signedInAs("admin"), "GET /runs/12": dead, "POST /runs/12/retry": { ...dead, status: "queued" } });
    renderApp("/runs/12");
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/Retry queued/)).toBeInTheDocument();
    expect(api.calls.some((c) => c.method === "POST" && c.url.pathname === "/runs/12/retry")).toBe(true);
  });

  it("shows the server's reason when a retry is refused", async () => {
    mockApi({
      ...signedInAs("admin"),
      "GET /runs/12": dead,
      "POST /runs/12/retry": () => ({ status: 409, body: { detail: "only failed or dead runs can be retried (this one is queued)" } }),
    });
    renderApp("/runs/12");
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/this one is queued/)).toBeInTheDocument();
  });

  it("does not offer retry to viewers, and says why", async () => {
    mockApi({ ...signedInAs("viewer"), "GET /runs/12": dead });
    renderApp("/runs/12");
    await screen.findByRole("heading", { name: /Run 12/ });
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
    expect(screen.getByText("Retrying needs an admin account.")).toBeInTheDocument();
  });

  it("does not offer retry for a run that already succeeded", async () => {
    mockApi({ ...signedInAs("admin"), "GET /runs/3": detail({ id: 3, status: "succeeded" }) });
    renderApp("/runs/3");
    await screen.findByRole("heading", { name: /Run 3/ });
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("shows a real not-found page for a run that doesn't exist", async () => {
    mockApi({ ...signedInAs("admin"), "GET /runs/999": () => ({ status: 404, body: { detail: "run not found" } }) });
    renderApp("/runs/999");
    expect(await screen.findByRole("heading", { name: "No such run" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to runs" })).toHaveAttribute("href", "/");
  });

  it("treats a nonsense id as not found without calling the API", async () => {
    const api = mockApi({ ...signedInAs("admin") });
    renderApp("/runs/abc");
    expect(await screen.findByRole("heading", { name: "No such run" })).toBeInTheDocument();
    expect(api.calls.some((c) => c.url.pathname.startsWith("/runs/abc"))).toBe(false);
  });
});

describe("not found and crashes", () => {
  it("shows a 404 page for an unknown address", async () => {
    mockApi({ ...signedInAs("admin") });
    renderApp("/nowhere/at/all");
    expect(await screen.findByRole("heading", { name: "No such page" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("the error boundary replaces a crashed page with a way forward", () => {
    function Bomb(): never {
      throw new Error("kaboom");
    }
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("This page crashed");
    expect(screen.getByText("kaboom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload page" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to runs" })).toBeInTheDocument();
  });

  it("a crash inside a page leaves the header and sign-out working", async () => {
    mockApi({ ...signedInAs("admin"), "POST /auth/logout": { status: 204 } });
    function Bomb(): never {
      throw new Error("page exploded");
    }
    render(
      <MemoryRouter initialEntries={["/boom"]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <AuthProvider>
          <Routes>
            <Route element={<Layout />}>
              <Route path="boom" element={<Bomb />} />
            </Route>
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    );
    expect(await screen.findByText("This page crashed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Automation Hub" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("renders children untouched when nothing is wrong", () => {
    render(
      <ErrorBoundary>
        <p>fine</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText("fine")).toBeInTheDocument();
  });
});
