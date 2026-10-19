import { useMutation, useQuery } from "@apollo/client/react";
import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router";

import { CREATE_TOKEN, DELETE_SERVICE, REGISTER_ROUTE, RETIRE_ROUTE, REVOKE_TOKEN, ROTATE_TOKEN, ROUTES, TOKENS } from "../api/operations";
import { useAuth } from "../auth/AuthContext";
import { SecretDialog } from "../components/SecretDialog";
import { Empty, Notice } from "../components/Status";
import { useReauthAction } from "../hooks/useReauthAction";
import { describeError } from "../lib/errors";
import { when } from "../lib/format";

export function SettingsTab({ serviceId, canManage }: { serviceId: string; canManage: boolean }) {
  const { session } = useAuth();
  return (
    <>
      <RoutesCard serviceId={serviceId} canManage={canManage} />
      {canManage && <TokensCard serviceId={serviceId} />}
      {session?.user.role === "ADMIN" && <DangerZone serviceId={serviceId} />}
    </>
  );
}

function RoutesCard({ serviceId, canManage }: { serviceId: string; canManage: boolean }) {
  const [template, setTemplate] = useState("");
  const { data, refetch } = useQuery(ROUTES, { variables: { serviceId, includeRetired: true }, fetchPolicy: "network-only" });
  const [register, reg] = useMutation(REGISTER_ROUTE);
  const [retire, ret] = useMutation(RETIRE_ROUTE);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const r = await register({ variables: { serviceId, template } }).catch(() => null);
    if (r?.data) {
      setTemplate("");
      void refetch();
    }
  };
  const routes = data?.routes.edges.map((e) => e.node) ?? [];
  return (
    <section className="card">
      <h2>Routes</h2>
      <p className="muted">Register static templates from your router definitions (e.g. <span className="mono">/users/{"{id}"}</span>), never real request URLs. Retire instead of editing.</p>
      {(reg.error || ret.error) && <Notice tone="error">{describeError(reg.error ?? ret.error)}</Notice>}
      {canManage && (
        <form onSubmit={submit} className="row" style={{ alignItems: "flex-end" }}>
          <div className="field" style={{ flex: 1, minWidth: 220 }}>
            <label htmlFor="route-template">Route template</label>
            <input id="route-template" maxLength={200} required value={template} onChange={(e) => setTemplate(e.target.value)} placeholder="/orders/{orderId}" />
          </div>
          <div className="field"><button type="submit" className="primary" disabled={!template}>Register route</button></div>
        </form>
      )}
      {routes.length === 0 ? <Empty>No routes registered.</Empty> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th scope="col">Template</th><th scope="col">Route ID (for emitters)</th><th scope="col">Status</th>{canManage && <th scope="col">Actions</th>}</tr></thead>
            <tbody>
              {routes.map((r) => (
                <tr key={r.id}>
                  <td className="mono">{r.template}</td>
                  <td className="mono">{r.id}</td>
                  <td>{r.retiredAt ? `Retired ${when(r.retiredAt)}` : "Active"}</td>
                  {canManage && <td>{!r.retiredAt && <button type="button" onClick={() => void retire({ variables: { id: r.id } }).then(() => refetch(), () => undefined)}>Retire</button>}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function TokensCard({ serviceId }: { serviceId: string }) {
  const [secret, setSecret] = useState<string | null>(null);
  const [failure, setFailure] = useState<unknown>(null);
  const { data, refetch } = useQuery(TOKENS, { variables: { id: serviceId }, fetchPolicy: "network-only" });
  // Secrets are never written to the Apollo cache.
  const [create] = useMutation(CREATE_TOKEN, { fetchPolicy: "no-cache" });
  const [rotate] = useMutation(ROTATE_TOKEN, { fetchPolicy: "no-cache" });
  const [revoke] = useMutation(REVOKE_TOKEN);
  const { run, dialog } = useReauthAction(setFailure);

  const issue = (fn: () => Promise<string | undefined>) =>
    run(async () => {
      setFailure(null);
      const value = await fn();
      if (value) setSecret(value);
      void refetch();
    }).catch(setFailure);

  const tokens = data?.service.ingestTokens ?? [];
  const now = Date.now();
  return (
    <section className="card">
      <h2>Ingest tokens</h2>
      <p className="muted">Service-scoped write-only credentials (max 2 active, 90-day expiry). Issuing, rotating and revoking require password confirmation.</p>
      {failure !== null && <Notice tone="error">{describeError(failure)}</Notice>}
      <button type="button" className="primary" onClick={() => void issue(async () => (await create({ variables: { serviceId } })).data?.createIngestToken.secret)}>Create token</button>
      {tokens.length === 0 ? <Empty>No tokens.</Empty> : (
        <div className="table-wrap" style={{ marginTop: 12 }}>
          <table>
            <thead><tr><th scope="col">Token ID</th><th scope="col">Created</th><th scope="col">Expires</th><th scope="col">Status</th><th scope="col">Actions</th></tr></thead>
            <tbody>
              {tokens.map((t) => {
                const active = !t.revokedAt && Date.parse(t.expiresAt) > now;
                return (
                  <tr key={t.id}>
                    <td className="mono">poi_{t.publicId}_…</td>
                    <td>{when(t.createdAt)}</td>
                    <td>{when(t.expiresAt)}</td>
                    <td>{t.revokedAt ? `Revoked ${when(t.revokedAt)}` : active ? "Active" : "Expired"}</td>
                    <td className="row">
                      {active && <button type="button" onClick={() => void issue(async () => (await rotate({ variables: { id: t.id } })).data?.rotateIngestToken.secret)}>Rotate</button>}
                      {active && <button type="button" className="danger" onClick={() => void run(async () => { await revoke({ variables: { id: t.id } }); void refetch(); }).catch(setFailure)}>Revoke</button>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <SecretDialog secret={secret} onClose={() => setSecret(null)} />
      {dialog}
    </section>
  );
}

function DangerZone({ serviceId }: { serviceId: string }) {
  const navigate = useNavigate();
  const [confirm, setConfirm] = useState(false);
  const [failure, setFailure] = useState<unknown>(null);
  const [del] = useMutation(DELETE_SERVICE);
  const { run, dialog } = useReauthAction(setFailure);
  return (
    <section className="card">
      <h2>Delete service</h2>
      <p className="muted">Immediately disables reads and ingestion and revokes tokens. Related data is purged within 24 hours. Backups may retain older copies until they expire (14 days).</p>
      {failure !== null && <Notice tone="error">{describeError(failure)}</Notice>}
      <label className="row" style={{ fontWeight: 400 }}><input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} /> I understand this cannot be undone</label>
      <button type="button" className="danger" disabled={!confirm} onClick={() => void run(async () => { await del({ variables: { id: serviceId } }); navigate("/"); }).catch(setFailure)}>Delete service</button>
      {dialog}
    </section>
  );
}
