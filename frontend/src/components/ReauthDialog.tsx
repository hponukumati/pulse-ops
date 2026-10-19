import { useState, type FormEvent } from "react";

import { reauthenticate } from "../auth/session";
import { messageFor } from "../lib/errors";
import { Dialog } from "./Dialog";

/** Recent-password confirmation (valid 5 minutes server-side) before sensitive actions. */
export function ReauthDialog({ open, onDone, onCancel }: { open: boolean; onDone: () => void; onCancel: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const r = await reauthenticate(password);
    setBusy(false);
    setPassword("");
    if (r.ok) onDone();
    else setError(messageFor(r.code));
  };
  return (
    <Dialog open={open} title="Confirm your password" onClose={onCancel}>
      <form onSubmit={submit}>
        <p className="secondary">This action requires a recent password confirmation.</p>
        <div className="field">
          <label htmlFor="reauth-password">Password</label>
          <input id="reauth-password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        {error && <p className="error-text" role="alert">{error}</p>}
        <div className="row">
          <button type="submit" className="primary" disabled={busy || !password}>Confirm</button>
          <button type="button" onClick={onCancel}>Cancel</button>
        </div>
      </form>
    </Dialog>
  );
}
