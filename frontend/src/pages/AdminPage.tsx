import { useMutation, useQuery } from "@apollo/client/react";
import { useState } from "react";

import { AUDIT, DIAGNOSTICS, GRANT, REVOKE_GRANT, SET_DISABLED, SET_RETENTION, SET_ROLE, USERS } from "../api/operations";
import { Loading, Notice } from "../components/Status";
import type { Role } from "../gql/graphql";
import { useReauthAction } from "../hooks/useReauthAction";
import { describeError } from "../lib/errors";
import { num, when } from "../lib/format";

const ROLES: Role[] = ["ADMIN", "OPERATOR", "VIEWER"];

export function AdminPage() {
  return (
    <>
      <h1>Administration</h1>
      <p className="muted">Account creation and password resets use the protected host CLI (see docs/OPERATIONS.md). Changes here require password confirmation.</p>
      <UsersCard />
      <SystemCard />
      <AuditCard />
    </>
  );
}

function UsersCard() {
  const { data, loading, error, refetch } = useQuery(USERS, { fetchPolicy: "network-only" });
  const [failure, setFailure] = useState<unknown>(null);
  const [setRole] = useMutation(SET_ROLE);
  const [setDisabled] = useMutation(SET_DISABLED);
  const [grant] = useMutation(GRANT);
  const [revokeGrant] = useMutation(REVOKE_GRANT);
  const { run, dialog } = useReauthAction(setFailure);
  const act = (fn: () => Promise<unknown>) =>
    void run(async () => {
      setFailure(null);
      await fn();
      await refetch();
    }).catch(setFailure);

  if (loading && !data) return <Loading />;
  const services = data?.services.edges.map((e) => e.node) ?? [];
  const grants = data?.serviceGrants.edges.map((e) => e.node) ?? [];
  return (
    <section className="card">
      <h2>Users and service access</h2>
      {(error || failure !== null) && <Notice tone="error">{describeError(error ?? failure)}</Notice>}
      <div className="table-wrap">
        <table>
          <thead><tr><th scope="col">Email</th><th scope="col">Role</th><th scope="col">Status</th><th scope="col">Service grants</th></tr></thead>
          <tbody>
            {data?.users.edges.map(({ node: u }) => {
              const mine = grants.filter((g) => g.userId === u.id);
              const available = services.filter((s) => !mine.some((g) => g.serviceId === s.id));
              return (
                <tr key={u.id}>
                  <td>{u.email}</td>
                  <td>
                    <label className="visually-hidden" htmlFor={`role-${u.id}`}>Role for {u.email}</label>
                    <select id={`role-${u.id}`} value={u.role} onChange={(e) => act(() => setRole({ variables: { userId: u.id, role: e.target.value as Role } }))}>
                      {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                    </select>
                  </td>
                  <td>
                    <button type="button" className={u.disabled ? "" : "danger"} onClick={() => act(() => setDisabled({ variables: { userId: u.id, disabled: !u.disabled } }))}>
                      {u.disabled ? "Enable" : "Disable"}
                    </button>
                  </td>
                  <td>
                    {u.role === "ADMIN" ? <span className="muted">All services</span> : (
                      <div className="stack">
                        {mine.map((g) => (
                          <div key={g.serviceId} className="row">
                            <span>{g.service?.name ?? g.serviceId}</span>
                            <button type="button" onClick={() => act(() => revokeGrant({ variables: { userId: u.id, serviceId: g.serviceId } }))}>Remove</button>
                          </div>
                        ))}
                        {available.length > 0 && (
                          <div>
                            <label className="visually-hidden" htmlFor={`grant-${u.id}`}>Grant a service to {u.email}</label>
                            <select id={`grant-${u.id}`} value="" onChange={(e) => e.target.value && act(() => grant({ variables: { userId: u.id, serviceId: e.target.value } }))}>
                              <option value="">Grant service…</option>
                              {available.map((s) => <option key={s.id} value={s.id}>{s.name} ({s.environment})</option>)}
                            </select>
                          </div>
                        )}
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {dialog}
    </section>
  );
}

function SystemCard() {
  const { data, error, refetch } = useQuery(DIAGNOSTICS, { fetchPolicy: "network-only", pollInterval: 15_000 });
  const [days, setDays] = useState<string>("");
  const [failure, setFailure] = useState<unknown>(null);
  const [setRetention] = useMutation(SET_RETENTION);
  const { run, dialog } = useReauthAction(setFailure);
  const d = data?.diagnostics;
  return (
    <section className="card">
      <h2>System health</h2>
      {(error || failure !== null) && <Notice tone="error">{describeError(error ?? failure)}</Notice>}
      {d && (
        <div className="kpis">
          <Item label="Database" value={d.databaseOk ? "OK" : "Unavailable"} />
          <Item label="Redis" value={d.redisOk ? "OK" : "Unavailable"} />
          <Item label="Worker heartbeat" value={when(d.lastWorkerHeartbeatAt)} />
          <Item label="Evaluations" value={d.evaluationsPaused ? `Paused (${d.pausedReason ?? "unknown"})` : "Running"} />
          <Item label="Outbox pending" value={num(d.outboxPending, 0)} />
          <Item label="Last dispatch OK" value={when(d.lastDispatchOkAt)} />
          <Item label="Last dispatch error" value={when(d.lastDispatchErrorAt)} />
          <Item label="Deletion jobs pending" value={num(d.deletionJobsPending, 0)} />
          <Item label="Retained events" value={`${num(d.retainedEvents, 0)} / ${num(d.retainedEventLimit, 0)}`} />
          <Item label="Event retention" value={`${d.eventRetentionDays} days`} />
        </div>
      )}
      <div className="row" style={{ alignItems: "flex-end" }}>
        <div>
          <label htmlFor="retention">Event retention (1–7 days)</label>
          <input id="retention" type="number" min={1} max={7} value={days} placeholder={d ? String(d.eventRetentionDays) : ""} onChange={(e) => setDays(e.target.value)} />
        </div>
        <button type="button" disabled={!days} onClick={() => void run(async () => { setFailure(null); await setRetention({ variables: { days: Number(days) } }); setDays(""); await refetch(); }).catch(setFailure)}>Save retention</button>
      </div>
      {dialog}
    </section>
  );
}

function AuditCard() {
  const { data, error, fetchMore } = useQuery(AUDIT, { variables: { after: null }, fetchPolicy: "network-only" });
  const [extra, setExtra] = useState<NonNullable<typeof data>["auditEvents"]["edges"]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const edges = [...(data?.auditEvents.edges ?? []), ...extra];
  const next = cursor ?? data?.auditEvents.pageInfo.endCursor ?? null;
  const hasMore = extra.length ? cursor !== null : !!data?.auditEvents.pageInfo.hasNextPage;
  const loadMore = async () => {
    const r = await fetchMore({ variables: { after: next } });
    setExtra((prev) => [...prev, ...r.data!.auditEvents.edges]);
    setCursor(r.data!.auditEvents.pageInfo.hasNextPage ? r.data!.auditEvents.pageInfo.endCursor : null);
  };
  return (
    <section className="card">
      <h2>Audit trail (30 days)</h2>
      {error && <Notice tone="error">{describeError(error)}</Notice>}
      <div className="table-wrap">
        <table>
          <thead><tr><th scope="col">Time</th><th scope="col">Action</th><th scope="col">Result</th><th scope="col">Actor</th><th scope="col">Target</th></tr></thead>
          <tbody>
            {edges.map(({ node: a }) => (
              <tr key={a.id}>
                <td>{when(a.occurredAt)}</td><td>{a.action}</td><td>{a.result}</td>
                <td className="mono">{a.actorKind}{a.actorId ? ` ${a.actorId.slice(0, 8)}…` : ""}</td>
                <td className="mono">{a.targetType ?? "—"}{a.targetId ? ` ${a.targetId.slice(0, 8)}…` : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {hasMore && <button type="button" onClick={() => void loadMore()}>Load more</button>}
    </section>
  );
}

function Item({ label, value }: { label: string; value: string }) {
  return <div className="kpi"><div className="label">{label}</div><div className="value" style={{ fontSize: "1rem" }}>{value}</div></div>;
}
