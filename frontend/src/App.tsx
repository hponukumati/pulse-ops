import { useQuery } from "@apollo/client/react";
import { Navigate, NavLink, Route, Routes } from "react-router";

import { ME } from "./api/operations";
import { useAuth } from "./auth/AuthContext";
import { Loading, Notice } from "./components/Status";
import { AccountPage } from "./pages/AccountPage";
import { AdminPage } from "./pages/AdminPage";
import { LoginPage } from "./pages/LoginPage";
import { OverviewPage } from "./pages/OverviewPage";
import { ServicePage } from "./pages/ServicePage";

export function App() {
  const { status, session } = useAuth();
  if (status === "loading") return <main><Loading label="Checking session…" /></main>;
  if (status !== "authenticated" || !session) {
    return (
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }
  return <Shell />;
}

function Shell() {
  const { session, logout } = useAuth();
  const { data } = useQuery(ME, { fetchPolicy: "network-only", pollInterval: 30_000 });
  const isAdmin = session?.user.role === "ADMIN";
  return (
    <>
      <a className="skip-link" href="#main">Skip to content</a>
      <header className="app-header">
        <NavLink to="/" className="brand">PulseOps</NavLink>
        <nav aria-label="Primary">
          <NavLink to="/" end>Overview</NavLink>
          {isAdmin && <NavLink to="/admin">Admin</NavLink>}
          <NavLink to="/account">Account</NavLink>
        </nav>
        <span className="header-spacer" />
        <span className="badge" title="Signed-in role">{data?.me.email ?? ""} · {session?.user.role}</span>
        <button type="button" onClick={() => void logout()}>Sign out</button>
      </header>
      <main id="main">
        {data?.systemStatus.evaluationStale && (
          <Notice tone="warning">
            Alert evaluation is stale{data.systemStatus.evaluationsPaused ? " (paused: notification backlog)" : ""}. Incident states may be out of date; silence does not mean recovery.
          </Notice>
        )}
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/services/:serviceId" element={<ServicePage />} />
          <Route path="/admin" element={isAdmin ? <AdminPage /> : <Navigate to="/" replace />} />
          <Route path="/account" element={<AccountPage />} />
          <Route path="/login" element={<Navigate to="/" replace />} />
          <Route path="*" element={<p>Page not found.</p>} />
        </Routes>
      </main>
    </>
  );
}
