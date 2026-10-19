import { useEffect, useRef } from "react";

/**
 * Poll while the page is visible; pause when hidden; refetch immediately when the page
 * becomes visible again or the browser comes back online.
 */
export function useVisiblePolling(refetch: () => unknown, intervalMs: number, enabled = true): void {
  const latest = useRef(refetch);
  latest.current = refetch;
  useEffect(() => {
    if (!enabled) return undefined;
    let timer: ReturnType<typeof setInterval> | undefined;
    const stop = () => {
      if (timer !== undefined) clearInterval(timer);
      timer = undefined;
    };
    const start = () => {
      stop();
      if (!document.hidden) timer = setInterval(() => void latest.current(), intervalMs);
    };
    const onVisibility = () => {
      if (document.hidden) stop();
      else {
        void latest.current();
        start();
      }
    };
    const onOnline = () => void latest.current();
    start();
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("online", onOnline);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("online", onOnline);
    };
  }, [intervalMs, enabled]);
}
