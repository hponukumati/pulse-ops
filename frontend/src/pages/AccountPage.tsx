import { useState, type FormEvent } from "react";

import { changePassword } from "../auth/session";
import { messageFor } from "../lib/errors";

export function AccountPage() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const r = await changePassword(current, next);
    setCurrent("");
    setNext("");
    setResult(r.ok ? { ok: true, text: "Password changed. Your other sessions were signed out." } : { ok: false, text: r.code === "VALIDATION_FAILED" ? "New password must be 15–128 characters and not a common password." : messageFor(r.code) });
  };
  return (
    <section className="card" style={{ maxWidth: 520 }}>
      <h1>Change password</h1>
      <form onSubmit={submit}>
        <div className="field">
          <label htmlFor="cur-pw">Current password</label>
          <input id="cur-pw" type="password" autoComplete="current-password" required value={current} onChange={(e) => setCurrent(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="new-pw">New password <span className="hint">15–128 characters; a passphrase works well</span></label>
          <input id="new-pw" type="password" autoComplete="new-password" minLength={15} maxLength={128} required value={next} onChange={(e) => setNext(e.target.value)} />
        </div>
        <button type="submit" className="primary" disabled={!current || !next}>Change password</button>
      </form>
      {result && <p className={result.ok ? "secondary" : "error-text"} role="status">{result.text}</p>}
    </section>
  );
}
