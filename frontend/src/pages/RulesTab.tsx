import { useMutation, useQuery } from "@apollo/client/react";
import { useState, type FormEvent } from "react";

import { CREATE_RULE, DELETE_RULE, RULES, UPDATE_RULE } from "../api/operations";
import { Empty, Loading, Notice } from "../components/Status";
import type { AlertMetric, Comparator } from "../gql/graphql";
import { describeError } from "../lib/errors";
import { ms, pct, when } from "../lib/format";

const METRICS: { value: AlertMetric; label: string; unit: string; hint: string }[] = [
  { value: "ERROR_RATE_5XX", label: "5xx error rate", unit: "fraction", hint: "0–1, e.g. 0.05 = 5%" },
  { value: "P95_LATENCY_MS", label: "p95 latency", unit: "ms", hint: "0–120000 ms" },
  { value: "REQUEST_RATE", label: "Request rate (high traffic)", unit: "req/min", hint: "requests per minute; not an outage detector" },
];
type Draft = { id?: string; name: string; metric: AlertMetric; comparator: Comparator; threshold: string; windowMinutes: number; minSamples: number; enabled: boolean };
const EMPTY: Draft = { name: "", metric: "ERROR_RATE_5XX", comparator: "GT", threshold: "0.05", windowMinutes: 5, minSamples: 20, enabled: true };

const show = (metric: string, v: number | null) => (metric === "ERROR_RATE_5XX" ? pct(v) : metric === "P95_LATENCY_MS" ? ms(v) : v == null ? "—" : `${v.toFixed(1)}/min`);

export function RulesTab({ serviceId, canManage }: { serviceId: string; canManage: boolean }) {
  const { data, loading, error, refetch } = useQuery(RULES, { variables: { serviceId }, fetchPolicy: "cache-and-network", pollInterval: 30_000 });
  const [draft, setDraft] = useState<Draft | null>(null);
  const [create, c] = useMutation(CREATE_RULE);
  const [update, u] = useMutation(UPDATE_RULE);
  const [remove, d] = useMutation(DELETE_RULE);
  const mutationError = c.error ?? u.error ?? d.error;

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!draft) return;
    const input = { name: draft.name, metric: draft.metric, comparator: draft.comparator, threshold: Number(draft.threshold), windowMinutes: draft.windowMinutes, minSamples: draft.minSamples, enabled: draft.enabled };
    const ok = draft.id ? await update({ variables: { id: draft.id, input } }).catch(() => null) : await create({ variables: { serviceId, input } }).catch(() => null);
    if (ok?.data) {
      setDraft(null);
      void refetch();
    }
  };
  const rules = data?.alertRules.edges.map((e) => e.node) ?? [];
  const meta = METRICS.find((m) => m.value === draft?.metric);

  return (
    <section className="card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>Alert rules</h2>
        {canManage && !draft && <button type="button" className="primary" onClick={() => setDraft({ ...EMPTY })}>New rule</button>}
      </div>
      <p className="muted">Evaluated every 60 s over a window ending 60 s earlier. Two consecutive breaches open an incident.</p>
      {(error || mutationError) && <Notice tone="error">{describeError(error ?? mutationError)}</Notice>}
      {loading && !data && <Loading />}
      {draft && (
        <form onSubmit={save} className="card" aria-label={draft.id ? "Edit rule" : "New rule"}>
          <div className="grid">
            <div className="field"><label htmlFor="r-name">Name</label><input id="r-name" required maxLength={80} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></div>
            <div className="field">
              <label htmlFor="r-metric">Metric</label>
              <select id="r-metric" value={draft.metric} onChange={(e) => setDraft({ ...draft, metric: e.target.value as AlertMetric })}>
                {METRICS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </select>
            </div>
            <div className="field">
              <label htmlFor="r-cmp">Comparator</label>
              <select id="r-cmp" value={draft.comparator} onChange={(e) => setDraft({ ...draft, comparator: e.target.value as Comparator })}>
                <option value="GT">greater than</option><option value="GTE">greater than or equal</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="r-thr">Threshold ({meta?.unit}) <span className="hint">{meta?.hint}</span></label>
              <input id="r-thr" inputMode="decimal" required value={draft.threshold} onChange={(e) => setDraft({ ...draft, threshold: e.target.value })} />
            </div>
            <div className="field">
              <label htmlFor="r-win">Window</label>
              <select id="r-win" value={draft.windowMinutes} onChange={(e) => setDraft({ ...draft, windowMinutes: Number(e.target.value) })}>
                {[5, 15, 60].map((w) => <option key={w} value={w}>{w} minutes</option>)}
              </select>
            </div>
            <div className="field"><label htmlFor="r-min">Minimum samples</label><input id="r-min" type="number" min={1} max={100000} value={draft.minSamples} onChange={(e) => setDraft({ ...draft, minSamples: Number(e.target.value) })} /></div>
          </div>
          <label className="row" style={{ fontWeight: 400 }}><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /> Enabled</label>
          <div className="row"><button type="submit" className="primary">Save rule</button><button type="button" onClick={() => setDraft(null)}>Cancel</button></div>
        </form>
      )}
      {data && rules.length === 0 && <Empty>No rules for this service.</Empty>}
      {rules.length > 0 && (
        <div className="table-wrap">
          <table>
            <caption className="visually-hidden">Alert rules</caption>
            <thead><tr><th scope="col">Name</th><th scope="col">Condition</th><th scope="col">Last evaluation</th><th scope="col">Streak</th>{canManage && <th scope="col">Actions</th>}</tr></thead>
            <tbody>
              {rules.map((r) => (
                <tr key={r.id}>
                  <td>{r.name}{!r.enabled && <span className="muted"> (disabled)</span>}</td>
                  <td>{METRICS.find((m) => m.value === r.metric)?.label} {r.comparator === "GT" ? ">" : "≥"} {show(r.metric, r.threshold)} over {r.windowMinutes} min (min {r.minSamples} samples)</td>
                  <td>{r.lastStatus ? `${r.lastStatus.toLowerCase().replace(/_/g, " ")} · ${show(r.metric, r.lastValue)} · ${when(r.lastEvaluatedSlot)}` : "not evaluated yet"}</td>
                  <td>{r.breachStreak ? `${r.breachStreak} breach` : r.okStreak ? `${r.okStreak} ok` : "—"}</td>
                  {canManage && (
                    <td className="row">
                      <button type="button" onClick={() => setDraft({ id: r.id, name: r.name, metric: r.metric, comparator: r.comparator, threshold: String(r.threshold), windowMinutes: r.windowMinutes, minSamples: r.minSamples, enabled: r.enabled })}>Edit</button>
                      <button type="button" className="danger" onClick={() => void remove({ variables: { id: r.id } }).then(() => refetch(), () => undefined)}>Delete</button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
