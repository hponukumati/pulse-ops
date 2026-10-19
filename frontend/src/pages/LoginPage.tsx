import { useState, type FormEvent } from "react";

import { useAuth } from "../auth/AuthContext";
import { messageFor } from "../lib/errors";

export function LoginPage() {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const r = await login(email, password);
    setBusy(false);
    setPassword("");
    if (!r.ok) setError(r.code === "RATE_LIMITED" && r.retryAfter ? `${messageFor(r.code)} (about ${Math.ceil(r.retryAfter / 60)} min)` : messageFor(r.code));
  };

  return (
    <main className="login">
      <div className="card">
        <h1>Sign in to PulseOps</h1>
        <p className="secondary">Accounts are provisioned by the workspace administrator.</p>
        <form onSubmit={submit} noValidate>
          <div className="field">
            <label htmlFor="email">Email</label>
            <input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="password">Password</label>
            <input id="password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
          {error && <p className="error-text" role="alert">{error}</p>}
          <button type="submit" className="primary" disabled={busy || !email || !password}>{busy ? "Signing in…" : "Sign in"}</button>
        </form>
      </div>
    </main>
  );
}
