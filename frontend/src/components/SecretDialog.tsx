import { useState } from "react";

import { Dialog } from "./Dialog";

/**
 * Shows a newly issued ingest credential exactly once. The value exists only in React state
 * for the life of this dialog: never stored, logged or sent anywhere except the clipboard
 * when the user explicitly asks. Exclude this flow from screenshots and recordings.
 */
export function SecretDialog({ secret, onClose }: { secret: string | null; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!secret) return;
    await navigator.clipboard.writeText(secret);
    setCopied(true);
  };
  const close = () => {
    setCopied(false);
    onClose();
  };
  return (
    <Dialog open={secret !== null} title="New ingest token" onClose={close}>
      <p>Copy this token now. It will not be shown again. Store it in your emitter's secret manager.</p>
      <code className="secret-box mono" data-testid="ingest-secret" data-sensitive="true">{secret}</code>
      <p className="muted" role="status">{copied ? "Copied to clipboard." : ""}</p>
      <div className="row">
        <button type="button" className="primary" onClick={() => void copy()}>Copy token</button>
        <button type="button" onClick={close}>I have stored it</button>
      </div>
    </Dialog>
  );
}
