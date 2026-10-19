import { useQuery } from "@apollo/client/react";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { SERVICE } from "../api/operations";
import { HealthBadge, Loading, Notice } from "../components/Status";
import { describeError } from "../lib/errors";
import { IncidentsTab } from "./IncidentsTab";
import { MetricsTab } from "./MetricsTab";
import { RulesTab } from "./RulesTab";
import { SettingsTab } from "./SettingsTab";

const TABS = ["Metrics", "Incidents", "Rules", "Settings"] as const;
type Tab = (typeof TABS)[number];

export function ServicePage() {
  const { serviceId = "" } = useParams();
  // Keyed by service id: switching services remounts every panel, so no previous data shows.
  return <ServiceView key={serviceId} serviceId={serviceId} />;
}

function ServiceView({ serviceId }: { serviceId: string }) {
  const [tab, setTab] = useState<Tab>("Metrics");
  const { data, loading, error } = useQuery(SERVICE, { variables: { id: serviceId }, fetchPolicy: "network-only" });
  if (loading && !data) return <Loading />;
  if (error || !data) {
    return (
      <>
        <Notice tone="error">{describeError(error)}</Notice>
        <Link to="/">Back to services</Link>
      </>
    );
  }
  const svc = data.service;
  return (
    <>
      <p><Link to="/">← Services</Link></p>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h1>{svc.name}</h1>
          <p className="muted">{svc.environment}</p>
        </div>
        <HealthBadge health={svc.health} />
      </div>
      <div className="tabs" role="tablist" aria-label="Service sections">
        {TABS.map((t) => (
          <button key={t} role="tab" id={`tab-${t}`} aria-selected={tab === t} aria-controls={`panel-${t}`} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === "Metrics" && <MetricsTab serviceId={svc.id} />}
        {tab === "Incidents" && <IncidentsTab serviceId={svc.id} canManage={svc.canManage} />}
        {tab === "Rules" && <RulesTab serviceId={svc.id} canManage={svc.canManage} />}
        {tab === "Settings" && <SettingsTab serviceId={svc.id} canManage={svc.canManage} />}
      </div>
    </>
  );
}
