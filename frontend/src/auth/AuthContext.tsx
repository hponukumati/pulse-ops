import { ApolloProvider } from "@apollo/client/react";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

import { createSessionClient, type SessionClient } from "../api/apollo";
import { Loading } from "../components/Status";
import { login, logout, onSessionChange, restore, type AuthResult, type SessionState } from "./session";

type AuthStatus = "loading" | "anonymous" | "authenticated";
type AuthValue = {
  status: AuthStatus;
  session: SessionState;
  login: (email: string, password: string) => Promise<AuthResult>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthValue | null>(null);

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth outside AuthProvider");
  return value;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [session, setSession] = useState<SessionState>(null);
  const [epoch, setEpoch] = useState(0);

  useEffect(() => {
    const off = onSessionChange((next, reason) => {
      setSession(next);
      setStatus(next ? "authenticated" : "anonymous");
      // New identity (or none): discard the Apollo client and all cached data.
      if (reason !== "refresh") setEpoch((e) => e + 1);
    });
    void restore().then((s) => {
      setSession(s);
      setStatus(s ? "authenticated" : "anonymous");
    });
    return off;
  }, []);

  const value: AuthValue = { status, session, login, logout };
  return (
    <AuthContext.Provider value={value}>
      {status === "authenticated" && session ? (
        <SessionApollo key={`${epoch}:${session.user.id}`}>{children}</SessionApollo>
      ) : (
        children
      )}
    </AuthContext.Provider>
  );
}

/** Owns one Apollo client for the lifetime of a session; disposing closes the socket and clears the cache. */
function SessionApollo({ children }: { children: ReactNode }) {
  const [sc, setSc] = useState<SessionClient | null>(null);
  useEffect(() => {
    const created = createSessionClient(() => void logout());
    setSc(created);
    return () => {
      void created.dispose();
    };
  }, []);
  if (!sc) return <Loading />;
  return <ApolloProvider client={sc.client}>{children}</ApolloProvider>;
}
