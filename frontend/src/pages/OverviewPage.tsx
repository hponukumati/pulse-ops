import { useMutation, useQuery } from "@apollo/client/react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router";

import { CREATE_SERVICE, SERVICES } from "../api/operations";
import { useAuth } from "../auth/AuthContext";
import { Empty, HealthBadge, Loading, Notice } from "../components/Status";
import { useVisiblePolling } from "../hooks/useVisiblePolling";
import { describeError } from "../lib/errors";
import { when } from "../lib/format";

export function OverviewPage() {
  const { session } = useAuth();
  const { data, loading, error, refetch } = useQuery(SERVICES, { fetchPolicy: "cache-and-network" });
  useVisiblePolling(refetch, 5000);
  const canCreate = session?.user.role === "ADMIN" || session?.user.role === "OPERATOR";
  const services = data?.services.edges.map((e) => e.node) ?? [];

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h1>Services</h1>
        <span className="muted">{data ? `${data.services.totalCount} visible to you` : ""}</span>
      </div>
      {error && <Notice tone="error">{describeError(error)}</Notice>}
      {loading && !data && <Loading />}
      {data && services.length === 0 && (
        <Empty>No services yet.{canCreate ? " Create one below." : " Ask an administrator for access."}</Empty>
      )}
      <div className="grid">
        {services.map((s) => (
          <article className="card" key={s.id}>
            <h2><Link to={`/services/${s.id}`}>{s.name}</Link></h2>
            <p className="muted">{s.environment}</p>
            <HealthBadge health={s.health} />
            <p className="secondary" style={{ marginTop: 8 }}>
              {s.health?.sampleCount15m ?? 0} requests in 15 min · last event {when(s.health?.lastEventAt)}
            </p>
            {!!s.health?.activeIncidents && <p><strong>{s.health.activeIncidents}</strong> active incident(s)</p>}
          </article>
        ))}
      </div>
      {canCreate && <CreateService onCreated={() => void refetch()} />}
    </>
  );
}

function CreateService({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [environment, setEnvironment] = useState("synthetic");
  const [create, { loading, error }] = useMutation(CREATE_SERVICE);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const r = await create({ variables: { input: { name, environment } } }).catch(() => null);
    if (r?.data) {
      setName("");
      onCreated();
    }
  };
  return (
    <section className="card" aria-labelledby="create-service">
      <h2 id="create-service">Create service</h2>
      <form onSubmit={submit} className="row" style={{ alignItems: "flex-end" }}>
        <div className="field">
          <label htmlFor="svc-name">Name <span className="hint">(plain text, no secrets or personal data)</span></label>
          <input id="svc-name" maxLength={80} required value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="svc-env">Environment</label>
          <input id="svc-env" maxLength={32} required value={environment} onChange={(e) => setEnvironment(e.target.value)} />
        </div>
        <div className="field"><button type="submit" className="primary" disabled={loading || !name}>Create</button></div>
      </form>
      {error && <p className="error-text" role="alert">{describeError(error)}</p>}
    </section>
  );
}
