import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useVisiblePolling } from "./useVisiblePolling";

function Probe({ fn }: { fn: () => void }) {
  useVisiblePolling(fn, 5000);
  return null;
}

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("useVisiblePolling", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    setHidden(false);
  });

  it("polls while visible, pauses when hidden, refetches on return and reconnect", () => {
    const fn = vi.fn();
    const { unmount } = render(<Probe fn={fn} />);
    vi.advanceTimersByTime(15_000);
    expect(fn).toHaveBeenCalledTimes(3);
    setHidden(true);
    vi.advanceTimersByTime(60_000);
    expect(fn).toHaveBeenCalledTimes(3);
    setHidden(false);
    expect(fn).toHaveBeenCalledTimes(4); // immediate refetch on becoming visible
    window.dispatchEvent(new Event("online"));
    expect(fn).toHaveBeenCalledTimes(5);
    unmount();
    vi.advanceTimersByTime(20_000);
    expect(fn).toHaveBeenCalledTimes(5);
  });
});
