import type { ReactNode } from "react";

type Health = { status: string; telemetryStale: boolean; evaluationStale: boolean; activeIncidents: number } | null | undefined;

const STATUS: Record<string, { tone: string; label: string; icon: string }> = {
  HEALTHY: { tone: "good", label: "Healthy", icon: "●" },
  DEGRADED: { tone: "critical", label: "Degraded", icon: "▲" },
  NO_DATA: { tone: "none", label: "No data", icon: "○" },
};

/** Status always pairs color with an icon and a text label. */
export function HealthBadge({ health }: { health: Health }) {
  if (!health) return <span className="badge">Unknown</span>;
  const s = STATUS[health.status] ?? STATUS.NO_DATA!;
  return (
    <span className="row" style={{ gap: 6 }}>
      <span className="badge"><span className={`dot ${s.tone}`} aria-hidden="true" />{s.label}</span>
      {health.telemetryStale && <span className="badge"><span className="dot warning" aria-hidden="true" />Telemetry stale</span>}
      {health.evaluationStale && <span className="badge"><span className="dot serious" aria-hidden="true" />Alerting stale</span>}
    </span>
  );
}

export function IncidentBadge({ status }: { status: string }) {
  const tone = status === "OPEN" ? "critical" : status === "ACKNOWLEDGED" ? "warning" : "good";
  const label = status === "OPEN" ? "Open" : status === "ACKNOWLEDGED" ? "Acknowledged" : "Resolved";
  return <span className="badge"><span className={`dot ${tone}`} aria-hidden="true" />{label}</span>;
}

export function Notice({ tone, children }: { tone: "info" | "warning" | "error"; children: ReactNode }) {
  return <div className={`notice ${tone}`} role={tone === "error" ? "alert" : "status"}>{children}</div>;
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return <p className="muted" role="status" aria-live="polite">{label}</p>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
