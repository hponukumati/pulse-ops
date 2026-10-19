import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { __resetForTests, currentSession, freshToken, login, logout, onSessionChange, refresh, type ChangeReason } from "./session";

const future = (ms: number) => new Date(Date.now() + ms).toISOString();
const sessionBody = (userId: string, ttl = 15 * 60_000) => ({
  accessToken: `access-${userId}-${Math.random()}`,
  accessTokenExpiresAt: future(ttl),
  sessionExpiresAt: future(12 * 3600_000),
  user: { id: userId, role: "OPERATOR" },
});
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });

describe("session", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    __resetForTests();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    localStorage.clear();
    sessionStorage.clear();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("keeps the access token in memory only", async () => {
    const setLocal = vi.spyOn(Storage.prototype, "setItem");
    fetchMock.mockResolvedValueOnce(ok(sessionBody("alice")));
    expect(await login("alice@pulseops.test", "pw")).toEqual({ ok: true });
    const token = currentSession()!.token;
    expect(setLocal).not.toHaveBeenCalled();
    expect(JSON.stringify({ ...localStorage })).not.toContain(token);
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(token);
    expect(document.cookie).not.toContain(token);
  });

  it("sends CSRF header and JSON to auth endpoints", async () => {
    fetchMock.mockResolvedValueOnce(ok(sessionBody("alice")));
    await login("alice@pulseops.test", "pw");
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/v1/auth/login");
    expect(init.method).toBe("POST");
    expect(init.headers["X-PulseOps-CSRF"]).toBe("1");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(init.credentials).toBe("same-origin");
  });

  it("deduplicates concurrent refreshes inside a tab", async () => {
    let resolve!: (r: Response) => void;
    fetchMock.mockReturnValueOnce(new Promise<Response>((r) => (resolve = r)));
    const a = refresh();
    const b = refresh();
    resolve(ok(sessionBody("alice")));
    await Promise.all([a, b]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("serializes refresh across tabs with a Web Lock", async () => {
    const request = vi.fn((_name: string, cb: () => Promise<unknown>) => cb());
    vi.stubGlobal("navigator", { ...navigator, locks: { request } });
    fetchMock.mockResolvedValueOnce(ok(sessionBody("alice")));
    await refresh();
    expect(request).toHaveBeenCalledWith("pulseops-refresh", expect.any(Function));
  });

  it("requires login again when refresh is rejected (replay/expiry)", async () => {
    const reasons: ChangeReason[] = [];
    onSessionChange((_s, r) => reasons.push(r));
    fetchMock.mockResolvedValueOnce(ok(sessionBody("alice"))).mockResolvedValueOnce(new Response("{}", { status: 401 }));
    await login("alice@pulseops.test", "pw");
    expect(await refresh()).toBeNull();
    expect(currentSession()).toBeNull();
    expect(reasons).toEqual(["login", "expired"]);
  });

  it("keeps the session when refresh fails transiently (503)", async () => {
    fetchMock.mockResolvedValueOnce(ok(sessionBody("alice"))).mockResolvedValueOnce(new Response("{}", { status: 503 }));
    await login("alice@pulseops.test", "pw");
    await expect(refresh()).rejects.toThrow();
    expect(currentSession()).not.toBeNull();
  });

  it("reports an identity change when the refreshed user differs", async () => {
    const reasons: ChangeReason[] = [];
    onSessionChange((_s, r) => reasons.push(r));
    fetchMock.mockResolvedValueOnce(ok(sessionBody("alice"))).mockResolvedValueOnce(ok(sessionBody("bob")));
    await login("alice@pulseops.test", "pw");
    await refresh();
    expect(reasons).toEqual(["login", "identity-changed"]);
  });

  it("refreshes proactively when the token is about to expire", async () => {
    fetchMock.mockResolvedValueOnce(ok(sessionBody("alice", 10_000))).mockResolvedValueOnce(ok(sessionBody("alice")));
    await login("alice@pulseops.test", "pw");
    const before = currentSession()!.token;
    const token = await freshToken();
    expect(token).not.toBe(before);
    expect(fetchMock.mock.calls[1]![0]).toBe("/api/v1/auth/refresh");
  });

  it("logout clears state first, notifies other tabs, and calls the server", async () => {
    const posted: unknown[] = [];
    const listener = new BroadcastChannel("pulseops-auth");
    listener.onmessage = (e) => posted.push(e.data);
    fetchMock.mockResolvedValueOnce(ok(sessionBody("alice"))).mockResolvedValueOnce(new Response(null, { status: 204 }));
    await login("alice@pulseops.test", "pw");
    await logout();
    expect(currentSession()).toBeNull();
    expect(fetchMock.mock.calls[1]![0]).toBe("/api/v1/auth/logout");
    await new Promise((r) => setTimeout(r, 20));
    expect(posted).toEqual(["logout"]);
    listener.close();
  });
});
