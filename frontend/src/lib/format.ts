/** Display formatting. Every value is rendered as text (no HTML injection paths). */
export const pct = (v: number | null | undefined): string => (v == null ? "—" : `${(v * 100).toFixed(v < 0.001 && v > 0 ? 3 : 1)}%`);
export const ms = (v: number | null | undefined): string => (v == null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${v.toFixed(v < 10 ? 1 : 0)} ms`);
export const num = (v: number | null | undefined, digits = 1): string => (v == null ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: digits }));
export const when = (iso: string | null | undefined): string => (iso ? new Date(iso).toLocaleString() : "—");
export const timeOnly = (iso: string): string => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
export const dayTime = (iso: string): string => new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
