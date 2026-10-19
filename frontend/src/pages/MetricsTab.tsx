import { useQuery } from "@apollo/client/react";
import { useState } from "react";

import { METRICS, ROUTES } from "../api/operations";
import { SeriesChart } from "../components/SeriesChart";
import { Loading, Notice } from "../components/Status";
import type { MetricsRange } from "../gql/graphql";
import { useVisiblePolling } from "../hooks/useVisiblePolling";
import { describeError } from "../lib/errors";
import { dayTime, ms, num, pct, timeOnly } from "../lib/format";
import { matchesSelection } from "../lib/stale";

const RANGES: { value: MetricsRange; label: string }[] = [
  { value: "LAST_15M", label: "Last 15 minutes" },
  { value: "LAST_1H", label: "Last hour" },
  { value: "LAST_24H", label: "Last 24 hours" },
  { value: "LAST_7D", label: "Last 7 days" },
];

export function MetricsTab({ serviceId }: { serviceId: string }) {
  const [range, setRange] = useState<MetricsRange>("LAST_1H");
  const [routeId, setRouteId] = useState<string | null>(null);
  const routes = useQuery(ROUTES, { variables: { serviceId, includeRetired: true } });
  const { data, error, refetch, loading } = useQuery(METRICS, {
    variables: { serviceId, range, routeId },
    fetchPolicy: "cache-and-network",
    notifyOnNetworkStatusChange: false,
  });
  useVisiblePolling(refetch, 5000);

  const m = data?.metrics;
  // Never render a response that belongs to a previous selection.
  const current = matchesSelection(m, { serviceId, range, routeId }) ? m : undefined;
  const short = range === "LAST_15M" || range === "LAST_1H";
  const points = (pick: (b: NonNullable<typeof current>["timeseries"][number]) => number | null) =>
    current?.timeseries.map((b) => ({ label: short ? timeOnly(b.start) : dayTime(b.start), value: pick(b), start: b.start, end: b.end })) ?? [];

  return (
    <>
      <div className="row" style={{ marginBottom: 16 }}>
        <div>
          <label htmlFor="range">Time range</label>
          <select id="range" value={range} onChange={(e) => setRange(e.target.value as MetricsRange)}>
            {RANGES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
          </select>
        </div>
        <div>
          <label htmlFor="route">Route</label>
          <select id="route" value={routeId ?? ""} onChange={(e) => setRouteId(e.target.value || null)}>
            <option value="">All routes</option>
            {routes.data?.routes.edges.map(({ node }) => (
              <option key={node.id} value={node.id}>{node.template}{node.retiredAt ? " (retired)" : ""}</option>
            ))}
          </select>
        </div>
        <p className="muted" style={{ alignSelf: "flex-end" }} aria-live="polite">
          {current ? `As of ${new Date(current.asOf).toLocaleTimeString()} · refreshes every 5 s while visible` : ""}
        </p>
      </div>
      {error && <Notice tone="error">{describeError(error)}</Notice>}
      {!current && loading && <Loading />}
      {current && (
        <>
          {current.status === "NO_DATA" && <Notice tone="info">No events in this window. Percentiles are unavailable until data arrives.</Notice>}
          <div className="kpis" aria-label="Summary metrics">
            <Kpi label="Requests" value={num(current.totalRequests, 0)} />
            <Kpi label="Requests / min" value={num(current.requestsPerMinute, 2)} />
            <Kpi label="5xx error rate" value={pct(current.errorRate5xx)} />
            <Kpi label="4xx rate" value={pct(current.clientErrorRate4xx)} />
            <Kpi label="p50 latency" value={ms(current.p50Ms)} />
            <Kpi label="p95 latency" value={ms(current.p95Ms)} />
            <Kpi label="p99 latency" value={ms(current.p99Ms)} />
            <Kpi label="Last event" value={current.lastEventAt ? dayTime(current.lastEventAt) : "—"} />
          </div>
          <div className="grid">
            <SeriesChart title="Requests per minute" kind="bar" points={points((b) => b.requestsPerMinute)} format={(v) => num(v, 2)} />
            <SeriesChart title="5xx error rate" kind="line" points={points((b) => (b.requests ? b.errorRate5xx : null))} format={pct} />
            <SeriesChart title="p95 latency" kind="line" points={points((b) => b.p95Ms)} format={ms} />
          </div>
          {!routeId && (
            <section className="card">
              <div className="table-wrap">
                <table>
                  <caption>Top endpoints</caption>
                  <thead>
                    <tr><th scope="col">Route</th><th scope="col">Method</th><th scope="col" className="num">Requests</th><th scope="col" className="num">5xx</th><th scope="col" className="num">4xx</th><th scope="col" className="num">p95</th></tr>
                  </thead>
                  <tbody>
                    {current.topEndpoints.length === 0 && <tr><td colSpan={6} className="muted">No traffic in this window.</td></tr>}
                    {current.topEndpoints.map((e) => (
                      <tr key={`${e.routeId}:${e.method}`}>
                        <td className="mono">{e.template}</td><td>{e.method}</td><td className="num">{num(e.requests, 0)}</td>
                        <td className="num">{pct(e.errorRate5xx)}</td><td className="num">{pct(e.clientErrorRate4xx)}</td><td className="num">{ms(e.p95Ms)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </>
      )}
    </>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="kpi">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
    </div>
  );
}
