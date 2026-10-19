import { useMutation, useQuery, useSubscription } from "@apollo/client/react";
import { useEffect, useRef, useState } from "react";

import { connectionEvents } from "../api/apollo";
import { ACK_INCIDENT, INCIDENT_CHANGED, INCIDENTS, RESOLVE_INCIDENT } from "../api/operations";
import { Empty, IncidentBadge, Loading, Notice } from "../components/Status";
import { useVisiblePolling } from "../hooks/useVisiblePolling";
import { describeError } from "../lib/errors";
import { ms, pct, when } from "../lib/format";

const fmtValue = (metric: string, v: number | null) => (metric === "ERROR_RATE_5XX" ? pct(v) : metric === "P95_LATENCY_MS" ? ms(v) : v == null ? "—" : `${v.toFixed(1)}/min`);

/**
 * Live incident list. Notifications are at-least-once and may be missed, so they only
 * trigger an authoritative refetch (deduplicated by incident id + version); the list is
 * also refetched on socket reconnect and every 30 seconds.
 */
export function IncidentsTab({ serviceId, canManage }: { serviceId: string; canManage: boolean }) {
  const [activeOnly, setActiveOnly] = useState(true);
  const { data, loading, error, refetch } = useQuery(INCIDENTS, { variables: { serviceId, activeOnly }, fetchPolicy: "cache-and-network" });
  const seen = useRef(new Map<string, number>());
  const sub = useSubscription(INCIDENT_CHANGED, {
    variables: { serviceId },
    onData: ({ data: payload }) => {
      const change = payload.data?.incidentChanged;
      if (!change) return;
      const prev = seen.current.get(change.incident.id) ?? 0;
      if (change.version <= prev) return; // duplicate notification, not a new transition
      seen.current.set(change.incident.id, change.version);
      void refetch();
    },
  });
  useVisiblePolling(refetch, 30_000);
  useEffect(() => {
    const onConnected = () => void refetch();
    connectionEvents.addEventListener("connected", onConnected);
    return () => connectionEvents.removeEventListener("connected", onConnected);
  }, [refetch]);

  const [ack, ackState] = useMutation(ACK_INCIDENT);
  const [resolve, resolveState] = useMutation(RESOLVE_INCIDENT);
  const mutationError = ackState.error ?? resolveState.error;
  const incidents = data?.incidents.edges.map((e) => e.node) ?? [];

  return (
    <section className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>Incidents</h2>
        <span className="badge" aria-live="polite">
          <span className={`dot ${sub.error ? "warning" : "good"}`} aria-hidden="true" />
          {sub.error ? "Live updates unavailable (refreshing every 30 s)" : "Live updates on"}
        </span>
      </div>
      <label className="row" style={{ fontWeight: 400 }}>
        <input type="checkbox" checked={activeOnly} onChange={(e) => setActiveOnly(e.target.checked)} /> Show active only
      </label>
      {(error || mutationError) && <Notice tone="error">{describeError(error ?? mutationError)}</Notice>}
      {loading && !data && <Loading />}
      {data && incidents.length === 0 && <Empty>{activeOnly ? "No active incidents." : "No incidents in the last 30 days."}</Empty>}
      {incidents.length > 0 && (
        <div className="table-wrap">
          <table>
            <caption className="visually-hidden">Incidents</caption>
            <thead>
              <tr><th scope="col">Status</th><th scope="col">Rule</th><th scope="col">Opened</th><th scope="col">Value / threshold</th><th scope="col">Acknowledged</th><th scope="col">Resolved</th>{canManage && <th scope="col">Actions</th>}</tr>
            </thead>
            <tbody>
              {incidents.map((i) => (
                <tr key={i.id}>
                  <td><IncidentBadge status={i.status} /></td>
                  <td>{i.rule?.name ?? "—"}</td>
                  <td>{when(i.openedAt)}</td>
                  <td>{fmtValue(i.metric, i.openedValue)} / {fmtValue(i.metric, i.threshold)}</td>
                  <td>{when(i.acknowledgedAt)}</td>
                  <td>{i.resolvedAt ? `${when(i.resolvedAt)} (${i.resolveReason?.toLowerCase().replace(/_/g, " ")})` : "—"}</td>
                  {canManage && (
                    <td className="row">
                      {i.status === "OPEN" && <button type="button" onClick={() => void ack({ variables: { id: i.id } }).then(() => refetch(), () => undefined)}>Acknowledge</button>}
                      {i.status !== "RESOLVED" && <button type="button" onClick={() => void resolve({ variables: { id: i.id } }).then(() => refetch(), () => undefined)}>Resolve</button>}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted">Acknowledging does not resolve. Two consecutive healthy evaluations resolve automatically; a manual resolve restarts breach counting.</p>
    </section>
  );
}
