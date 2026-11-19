import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, api, type RunDetail as Detail } from "../api";
import { useAuth } from "../auth";
import { ErrorNotice, StatusPlate } from "../components";
import { formatDateTime, formatDuration, statusNote } from "../format";
import { useLoader } from "../hooks";
import NotFound from "./NotFound";

const RETRYABLE = ["failed", "dead"];
const REFRESH_MS = 5_000;

export default function RunDetail() {
  const { id } = useParams();
  const runId = Number(id);
  const valid = Number.isInteger(runId) && runId > 0;
  const { user } = useAuth();
  const run = useLoader(() => api.run(runId), `run|${id}`, valid ? REFRESH_MS : 0);
  const [retrying, setRetrying] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  if (!valid || (run.error instanceof ApiError && run.error.status === 404)) return <NotFound what="run" />;
  if (run.error && !run.data) return <main className="page"><ErrorNotice error={run.error} onRetry={run.reload} /></main>;
  if (!run.data) return <main className="page"><p className="status-line" role="status">Loading run {runId}</p></main>;

  const data: Detail = run.data;
  const canRetry = user?.role === "admin" && RETRYABLE.includes(data.status);

  async function retry() {
    setRetrying(true);
    setMessage(null);
    try {
      await api.retry(runId);
      setMessage({ ok: true, text: "Retry queued. A worker will pick it up in a few seconds." });
      run.reload();
    } catch (e) {
      setMessage({ ok: false, text: e instanceof Error ? e.message : "The retry didn't go through." });
    } finally {
      setRetrying(false);
    }
  }

  const note = statusNote(data);
  return (
    <main className="page detail">
      <p className="crumb">
        <Link to="/">Runs</Link>
      </p>
      <div className="detail-head">
        <h1>
          Run {data.id} <span className="from">from {data.source}</span>
        </h1>
        <StatusPlate status={data.status} note={note} />
      </div>

      <div className="actions">
        {canRetry ? (
          <button type="button" className="button primary" onClick={() => void retry()} disabled={retrying}>
            {retrying ? "Retrying" : "Retry"}
          </button>
        ) : null}
        {!canRetry && user?.role === "viewer" && RETRYABLE.includes(data.status) ? (
          <span className="hint">Retrying needs an admin account.</span>
        ) : null}
        <span className="message" role="status" data-ok={message?.ok}>
          {message?.text}
        </span>
      </div>

      {data.last_error ? (
        <section aria-labelledby="h-error" className="callout">
          <h2 id="h-error">{data.status === "dead" ? "Why it gave up" : "Last error"}</h2>
          <pre>{data.last_error}</pre>
        </section>
      ) : null}

      <section aria-labelledby="h-facts">
        <h2 id="h-facts">Details</h2>
        <dl className="facts">
          <div><dt>Idempotency key</dt><dd className="wrap">{data.idempotency_key}</dd></div>
          <div><dt>Tries used</dt><dd>{data.attempts} of {data.max_attempts}</dd></div>
          <div><dt>Received</dt><dd>{formatDateTime(data.created_at)}</dd></div>
          <div>
            <dt>{data.finished_at ? "Finished" : "Next try"}</dt>
            <dd>{data.finished_at ? formatDateTime(data.finished_at) : data.status === "running" ? "In progress" : formatDateTime(data.run_at)}</dd>
          </div>
        </dl>
      </section>

      <section aria-labelledby="h-attempts">
        <h2 id="h-attempts">Tries</h2>
        {data.attempts_log.length === 0 ? (
          <p className="hint">No try has started yet.</p>
        ) : (
          <ol className="attempts">
            {data.attempts_log.map((a) => (
              <li key={a.attempt_no} className={a.ok === false ? "bad" : a.ok ? "good" : "live"}>
                <span className="a-no">{a.attempt_no}</span>
                <span className="a-main">
                  <strong>{a.ok === null ? "Running" : a.ok ? "Succeeded" : "Failed"}</strong>
                  <span className="a-meta">
                    <span>{formatDateTime(a.started_at)}</span>
                    <span>{formatDuration(a.duration_ms)}</span>
                    <span>worker {a.worker_id}</span>
                  </span>
                  {a.error ? <pre>{a.error}</pre> : null}
                </span>
              </li>
            ))}
          </ol>
        )}
      </section>

      <section aria-labelledby="h-payload">
        <h2 id="h-payload">Received payload</h2>
        <pre className="payload" tabIndex={0}>{JSON.stringify(data.event.payload, null, 2)}</pre>
      </section>

      <section aria-labelledby="h-audit">
        <h2 id="h-audit">History</h2>
        {data.audit.length === 0 ? (
          <p className="hint">Nothing recorded yet.</p>
        ) : (
          <table className="audit">
            <thead>
              <tr><th scope="col">When</th><th scope="col">Who</th><th scope="col">What</th></tr>
            </thead>
            <tbody>
              {data.audit.map((e) => (
                <tr key={e.id}>
                  <td>{formatDateTime(e.at)}</td>
                  <td>{e.actor}</td>
                  <td>{e.action}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}
