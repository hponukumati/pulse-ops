/**
 * Guards against showing a response that belongs to a previous selection (e.g. the user
 * switched service or range while a request was in flight).
 */
export type MetricsKey = { serviceId: string; range: string; routeId: string | null };

export function matchesSelection(data: { serviceId: string; range: string; routeId: string | null } | null | undefined, want: MetricsKey): boolean {
  return !!data && data.serviceId === want.serviceId && data.range === want.range && (data.routeId ?? null) === (want.routeId ?? null);
}

/** Monotonic request tokens: only the latest request's result may be applied. */
export function latestOnly(): { next: () => number; isCurrent: (token: number) => boolean } {
  let counter = 0;
  return { next: () => ++counter, isCurrent: (token) => token === counter };
}
