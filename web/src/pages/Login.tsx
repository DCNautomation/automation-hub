import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "../api";
import { useAuth } from "../auth";

function explain(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "That username and password don't match.";
    if (error.status === 429) {
      const minutes = Math.max(1, Math.ceil((error.retryAfter ?? 60) / 60));
      return `Too many failed attempts. Try again in ${minutes} ${minutes === 1 ? "minute" : "minutes"}.`;
    }
    if (error.status === 0) return error.message + " Check the stack is running.";
    return error.message;
  }
  return "Sign-in failed. Try again.";
}

export default function Login() {
  const { user, signIn } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? "/";
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to={from} replace />;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(username.trim(), password);
      navigate(from, { replace: true });
    } catch (e) {
      setError(explain(e));
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <h1>Automation Hub</h1>
      <p className="lede">Sign in to see what ran, what failed, and what's waiting.</p>
      <form onSubmit={submit} noValidate>
        <label>
          Username
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        <p className="form-error" role="alert" hidden={!error}>
          {error}
        </p>
        <button type="submit" className="button primary" disabled={busy || !username.trim() || !password}>
          {busy ? "Signing in" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
