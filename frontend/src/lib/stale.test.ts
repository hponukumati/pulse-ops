import { describe, expect, it } from "vitest";

import { latestOnly, matchesSelection } from "./stale";

describe("stale response guards", () => {
  const want = { serviceId: "A", range: "LAST_1H", routeId: null };
  it("accepts only data for the current selection", () => {
    expect(matchesSelection({ serviceId: "A", range: "LAST_1H", routeId: null }, want)).toBe(true);
    expect(matchesSelection({ serviceId: "B", range: "LAST_1H", routeId: null }, want)).toBe(false);
    expect(matchesSelection({ serviceId: "A", range: "LAST_7D", routeId: null }, want)).toBe(false);
    expect(matchesSelection({ serviceId: "A", range: "LAST_1H", routeId: "r1" }, want)).toBe(false);
    expect(matchesSelection(undefined, want)).toBe(false);
  });
  it("lets only the latest request win", () => {
    const g = latestOnly();
    const first = g.next();
    const second = g.next();
    expect(g.isCurrent(first)).toBe(false);
    expect(g.isCurrent(second)).toBe(true);
  });
});
