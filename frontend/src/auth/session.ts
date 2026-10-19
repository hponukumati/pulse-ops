/**
 * Browser session state.
 *
 * - The access JWT lives only in this module's memory: never localStorage, sessionStorage,
 *   IndexedDB, cookies set by script, or a persisted Apollo cache.
 * - The refresh secret is an HttpOnly cookie the script cannot read.
 * - Refreshes are serialized across tabs with the Web Locks API so two tabs never present
 *   the same refresh cookie; a replay/race on the server means "log in again".
 * - Logout clears state here and in other tabs (BroadcastChannel carries no secrets).
 */

export type Role = "ADMIN" | "OPERATOR" | "VIEWER";
export type SessionUser = { id: string; role: Role };
export type SessionState = {
  token: string;
  expiresAt: number;
  sessionExpiresAt: number;
  user: SessionUser;
} | null;
export type ChangeReason = "login" | "refresh" | "logout" | "expired" | "identity-changed" | "remote-logout";
type Listener = (state: SessionState, reason: ChangeReason) => void;

export type AuthResult = { ok: true } | { ok: false; code: string; retryAfter?: number };

const JSON_CSRF = { "Content-Type": "application/json", "X-PulseOps-CSRF": "1" };
const REFRESH_MARGIN_MS = 30_000;

let state: SessionState = null;
let inflight: Promise<SessionState> | null = null;
const listeners = new Set<Listener>();
const channel = typeof BroadcastChannel !== "undefined" ? new BroadcastChannel("pulseops-auth") : null;

channel?.addEventListener("message", (event: MessageEvent) => {
  if (event.data === "logout") setState(null, "remote-logout");
});

function setState(next: SessionState, reason: ChangeReason): void {
  const previous = state;
  state = next;
  const effective = previous && next && previous.user.id !== next.user.id ? "identity-changed" : reason;
  listeners.forEach((fn) => fn(next, effective));
}

export function currentSession(): SessionState {
  return state;
}

export function onSessionChange(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

async function post(path: string, body: unknown, token?: string): Promise<Response> {
  return fetch(path, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: token ? { ...JSON_CSRF, Authorization: `Bearer ${token}` } : JSON_CSRF,
    body: JSON.stringify(body),
  });
}

type SessionJson = {
  accessToken: string;
  accessTokenExpiresAt: string;
  sessionExpiresAt: string;
  user: SessionUser;
};

function toState(json: SessionJson): SessionState {
  return {
    token: json.accessToken,
    expiresAt: Date.parse(json.accessTokenExpiresAt),
    sessionExpiresAt: Date.parse(json.sessionExpiresAt),
    user: json.user,
  };
}

async function failure(r: Response): Promise<AuthResult> {
  let code = "UNKNOWN";
  try {
    code = ((await r.json()) as { error?: { code?: string } }).error?.code ?? code;
  } catch {
    /* non-JSON error body */
  }
  const retry = Number(r.headers.get("Retry-After"));
  return Number.isFinite(retry) && retry > 0 ? { ok: false, code, retryAfter: retry } : { ok: false, code };
}

export async function login(email: string, password: string): Promise<AuthResult> {
  const r = await post("/api/v1/auth/login", { email, password });
  if (!r.ok) return failure(r);
  setState(toState((await r.json()) as SessionJson), "login");
  return { ok: true };
}

type LockManagerLike = { request<T>(name: string, cb: () => Promise<T>): Promise<T> };

async function withRefreshLock<T>(fn: () => Promise<T>): Promise<T> {
  const locks = (globalThis.navigator as { locks?: LockManagerLike } | undefined)?.locks;
  return locks ? locks.request("pulseops-refresh", fn) : fn();
}

/** Rotate the refresh cookie and obtain a new access token. Resolves null when login is required. */
export function refresh(): Promise<SessionState> {
  if (inflight) return inflight;
  inflight = withRefreshLock(async () => {
    const r = await post("/api/v1/auth/refresh", {});
    if (r.ok) {
      const next = toState((await r.json()) as SessionJson);
      setState(next, "refresh");
      return next;
    }
    if (r.status === 401 || r.status === 403) {
      if (state) setState(null, "expired");
      return null;
    }
    throw new Error(`refresh unavailable (${r.status})`); // 429/503: keep state; caller may retry
  }).finally(() => {
    inflight = null;
  });
  return inflight;
}

/** A valid access token, refreshing first if it is about to expire. */
export async function freshToken(): Promise<string | null> {
  if (state && state.expiresAt - Date.now() > REFRESH_MARGIN_MS) return state.token;
  if (!state) return null;
  const next = await refresh();
  return next?.token ?? null;
}

/** Restore a session from the refresh cookie on page load (no-op if none). */
export async function restore(): Promise<SessionState> {
  try {
    return await refresh();
  } catch {
    return null;
  }
}

export async function logout(): Promise<void> {
  const token = state?.token;
  setState(null, "logout");
  channel?.postMessage("logout");
  try {
    await post("/api/v1/auth/logout", {}, token);
  } catch {
    /* the local session is already cleared; the server session expires on its own */
  }
}

export async function reauthenticate(password: string): Promise<AuthResult> {
  const token = await freshToken();
  if (!token) return { ok: false, code: "UNAUTHENTICATED" };
  const r = await post("/api/v1/auth/reauthenticate", { password }, token);
  return r.ok ? { ok: true } : failure(r);
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<AuthResult> {
  const token = await freshToken();
  if (!token) return { ok: false, code: "UNAUTHENTICATED" };
  const r = await post("/api/v1/auth/password", { currentPassword, newPassword }, token);
  return r.ok ? { ok: true } : failure(r);
}

/** Test hook: reset module state between unit tests. */
export function __resetForTests(): void {
  state = null;
  inflight = null;
  listeners.clear();
}
