import { Component, type ErrorInfo, type ReactNode } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { ApiError, API_URL, type Status } from "./api";
import { useAuth } from "./auth";
import { DISPLAY_TZ, STATUS_LABEL } from "./format";

/** Catches a crash in any page below it so one bad render doesn't blank the whole app. */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Page crashed:", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="page narrow" role="alert">
        <h1>This page crashed</h1>
        <p>Nothing was lost. Reload to try again, or go back to the runs list.</p>
        <pre className="error-detail">{this.state.error.message}</pre>
        <div className="actions">
          <button type="button" className="button" onClick={() => window.location.reload()}>
            Reload page
          </button>
          <a className="button quiet" href="/">
            Back to runs
          </a>
        </div>
      </main>
    );
  }
}

export function StatusPlate({ status, note }: { status: Status; note?: string | null }) {
  return (
    <span className="plate-wrap">
      <span className={`plate plate-${status}`}>{STATUS_LABEL[status]}</span>
      {note ? <span className="plate-note">{note}</span> : null}
    </span>
  );
}

/** What went wrong and what to do about it, in the interface's voice. */
export function ErrorNotice({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  const down = error instanceof ApiError && error.status === 0;
  return (
    <div className="notice" role="alert">
      <p className="notice-title">{down ? "The API isn't answering" : "That didn't load"}</p>
      <p>
        {down
          ? `Nothing replied at ${API_URL}. Check the stack is running (docker compose up) and that this site's address is in the API's CORS_ORIGINS.`
          : error.message}
      </p>
      {onRetry ? (
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  );
}

/** Demo guide at the very bottom of every signed-in page: what to click on each tab and what it proves. */
function WhatToTry() {
  const items: { tab: string; steps: string }[] = [
    { tab: "Runs: All", steps: "Scan the board. Every webhook the hub ever received is one row, newest first. Use the source and date filters, then Clear filters." },
    { tab: "Runs: Needs attention", steps: "Shows runs that are retrying or gave up. These are the ones an on-call person would care about. The count updates by itself." },
    { tab: "Runs: Retrying and Gave up", steps: "Retrying means a failed run is waiting for its next try with a growing delay. Gave up means every try failed." },
    { tab: "Open any run", steps: "See the error, every try, the payload it received and the full history. Nothing is ever deleted from the history." },
    { tab: "Retry button (admin)", steps: "On a run that gave up, press Retry. It goes back in the queue and a worker picks it up in a few seconds. Viewers do not see this button." },
    { tab: "Send a new event", steps: "Call the n8n production webhook URL from your site or a tool like Postman. The signed event lands here as a new run within seconds. Send the same key twice and it only runs once." },
  ];
  return (
    <section className="what-to-try" aria-labelledby="h-try">
      <h2 id="h-try">WHAT TO TRY</h2>
      <p className="what-to-try-lead">A quick tour of what this demo can do. Go tab by tab.</p>
      <div className="what-to-try-list">
        {items.map((it) => (
          <div className="what-to-try-item" key={it.tab}>
            <strong>{it.tab}</strong>
            <span>{it.steps}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

export function Layout() {
  const { user, signOut } = useAuth();
  return (
    <>
      <a className="skip" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <Link to="/" className="wordmark">
          Automation Hub
        </Link>
        <nav aria-label="Main">
          <NavLink to="/" end>
            Runs
          </NavLink>
        </nav>
        <div className="who">
          <span className="who-name">
            {user?.username} <span className="who-role">{user?.role}</span>
          </span>
          <button type="button" className="button quiet small" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>
      <div id="main">
        <ErrorBoundary>
          <Outlet />
        </ErrorBoundary>
      </div>
      <WhatToTry />
      <footer className="foot">Times are shown in Philippine time ({DISPLAY_TZ}).</footer>
    </>
  );
}
