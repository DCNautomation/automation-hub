import { useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Status } from "../api";
import { ErrorNotice, StatusPlate } from "../components";
import { RANGES, STATUS_LABEL, STATUS_ORDER, formatWhen, rangeToSince, statusNote } from "../format";
import { useLoader } from "../hooks";

const PAGE_SIZE = 25;
const ATTENTION: Status[] = ["failed", "dead"];
const REFRESH_MS = 5_000;

type View = "all" | "attention" | Status;

/** The filter the URL describes. Everything lives in the query string so a view can be linked. */
function readView(params: URLSearchParams): { view: View; statuses: Status[] } {
  const wanted = params.getAll("status").filter((s): s is Status => STATUS_ORDER.includes(s as Status));
  if (wanted.length === 0) return { view: "all", statuses: [] };
  if (wanted.length === 2 && ATTENTION.every((s) => wanted.includes(s))) return { view: "attention", statuses: ATTENTION };
  if (wanted.length === 1) return { view: wanted[0], statuses: wanted };
  return { view: "all", statuses: wanted };
}

export default function Runs() {
  const [params, setParams] = useSearchParams();
  const { view, statuses } = readView(params);
  const source = params.get("source") ?? "";
  const range = params.get("range") ?? "all";
  const before = Number(params.get("before")) || undefined;

  const key = params.toString();
  const sources = useLoader(() => api.sources(), "sources");
  const summary = useLoader(() => api.summary(rangeToSince(range)), `summary|${range}`, REFRESH_MS);
  // Only the newest page refreshes by itself: a page someone scrolled to should hold still.
  const runs = useLoader(
    () => api.runs({ status: statuses, source: source || undefined, since: rangeToSince(range), before, limit: PAGE_SIZE }),
    `runs|${key}`,
    before ? 0 : REFRESH_MS,
  );

  function update(next: Record<string, string | string[] | null>) {
    const copy = new URLSearchParams(params);
    copy.delete("before");
    for (const [name, value] of Object.entries(next)) {
      copy.delete(name);
      if (value === null || value === "" || value === "all") continue;
      for (const v of Array.isArray(value) ? value : [value]) copy.append(name, v);
    }
    setParams(copy);
  }

  const counts = summary.data?.counts;
  const attentionCount = counts ? counts.failed + counts.dead : null;
  const tabs = useMemo(
    () => [
      { id: "all" as View, label: "All", count: summary.data?.total ?? null, statuses: null as string[] | null },
      { id: "attention" as View, label: "Needs attention", count: attentionCount, statuses: ATTENTION as string[] },
      ...STATUS_ORDER.map((s) => ({ id: s as View, label: STATUS_LABEL[s], count: counts ? counts[s] : null, statuses: [s] as string[] })),
    ],
    [summary.data, counts, attentionCount],
  );

  const filtered = view !== "all" || statuses.length > 0 || source !== "" || range !== "all";
  const now = new Date();

  return (
    <main className="page">
      <h1 className="visually-hidden">Runs</h1>

      <div className="filters">
        <div className="tabs" role="group" aria-label="Filter by state">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              className={`tab${view === tab.id ? " on" : ""}${tab.id === "attention" && attentionCount ? " alert" : ""}`}
              aria-pressed={view === tab.id}
              onClick={() => update({ status: tab.statuses })}
            >
              {tab.label}
              <span className="tab-count">{tab.count ?? ""}</span>
            </button>
          ))}
        </div>
        <div className="selects">
          <label>
            Source
            <select value={source} onChange={(e) => update({ source: e.target.value })}>
              <option value="">All sources</option>
              {(sources.data ?? []).map((s) => (
                <option key={s.id} value={s.name}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Received
            <select value={range} onChange={(e) => update({ range: e.target.value })}>
              {RANGES.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      {runs.error ? (
        <ErrorNotice error={runs.error} onRetry={runs.reload} />
      ) : runs.loading && !runs.data ? (
        <p className="status-line" role="status">
          Loading runs
        </p>
      ) : runs.data && runs.data.items.length === 0 ? (
        <div className="empty">
          {filtered ? (
            <>
              <p className="empty-title">No runs match these filters</p>
              <p>Widen the time range or pick another state.</p>
              <button type="button" className="button" onClick={() => setParams(new URLSearchParams())}>
                Clear filters
              </button>
            </>
          ) : (
            <>
              <p className="empty-title">No runs yet</p>
              <p>
                Send a signed webhook to <code>POST /ingest/&#123;source&#125;</code> and it will show up here within a few seconds.
              </p>
            </>
          )}
        </div>
      ) : runs.data ? (
        <>
          <table className="board">
            <caption className="visually-hidden">Runs, newest first</caption>
            <thead>
              <tr>
                <th scope="col">Run</th>
                <th scope="col">Source</th>
                <th scope="col">Tries</th>
                <th scope="col">State</th>
                <th scope="col">Received</th>
              </tr>
            </thead>
            <tbody>
              {runs.data.items.map((run) => (
                <tr key={run.id} className={`row row-${run.status}`}>
                  <td className="c-id">
                    <Link to={`/runs/${run.id}`} aria-label={`Open run ${run.id}`}>
                      {run.id}
                    </Link>
                  </td>
                  <td className="c-source">
                    <span className="source">{run.source}</span>
                    <span className="key">{run.idempotency_key}</span>
                    {run.last_error ? (
                      <span className="err" title={run.last_error}>
                        {run.last_error.replace(/^RuntimeError:\s*/, "")}
                      </span>
                    ) : null}
                  </td>
                  <td className="c-tries">
                    {run.attempts}
                    <span className="of">/{run.max_attempts}</span>
                  </td>
                  <td className="c-state">
                    <StatusPlate status={run.status} note={statusNote(run, now)} />
                  </td>
                  <td className="c-when">{formatWhen(run.created_at, now)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <nav className="pager" aria-label="Pages">
            {before ? (
              <button type="button" className="button quiet" onClick={() => update({})}>
                Back to newest
              </button>
            ) : (
              <span />
            )}
            {runs.data.next_before ? (
              <button type="button" className="button" onClick={() => setParams(withBefore(params, runs.data!.next_before!))}>
                Older runs
              </button>
            ) : (
              <span className="end">End of the list</span>
            )}
          </nav>
        </>
      ) : null}
    </main>
  );
}

function withBefore(params: URLSearchParams, before: number) {
  const copy = new URLSearchParams(params);
  copy.set("before", String(before));
  return copy;
}
