import { useCallback, useRef, useState, type ReactElement } from "react";

import { ReauthDialog } from "../components/ReauthDialog";
import { errorCodes } from "../lib/errors";

/**
 * Runs a sensitive action; if the server answers REAUTH_REQUIRED, asks for the password
 * (server-side 5-minute reauthentication) and retries the same action once.
 */
export function useReauthAction(onError?: (error: unknown) => void): { run: (action: () => Promise<unknown>) => Promise<unknown>; dialog: ReactElement } {
  const pending = useRef<(() => Promise<unknown>) | null>(null);
  const [open, setOpen] = useState(false);
  const run = useCallback(async (action: () => Promise<unknown>) => {
    try {
      return await action();
    } catch (err) {
      if (errorCodes(err).includes("REAUTH_REQUIRED")) {
        pending.current = action;
        setOpen(true);
        return undefined;
      }
      throw err;
    }
  }, []);
  const dialog = (
    <ReauthDialog
      open={open}
      onCancel={() => {
        pending.current = null;
        setOpen(false);
      }}
      onDone={() => {
        setOpen(false);
        const action = pending.current;
        pending.current = null;
        // Errors from the retried action (e.g. a token limit) must still reach the user.
        void action?.().catch((err: unknown) => onError?.(err));
      }}
    />
  );
  return { run, dialog };
}
