import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const created: { dispose: ReturnType<typeof vi.fn> }[] = [];
vi.mock("../api/apollo", () => ({
  connectionEvents: new EventTarget(),
  createSessionClient: () => {
    const entry = { client: { __test: created.length }, dispose: vi.fn(async () => undefined), ws: {} };
    created.push(entry);
    return entry;
  },
}));
vi.mock("@apollo/client/react", () => ({
  ApolloProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

import { __resetForTests, login, logout } from "./session";
import { AuthProvider, useAuth } from "./AuthContext";

function WhoAmI() {
  const { session, status } = useAuth();
  return <p>{status}:{session?.user.id ?? "none"}</p>;
}

const body = (id: string) => JSON.stringify({
  accessToken: `t-${id}`, accessTokenExpiresAt: new Date(Date.now() + 900_000).toISOString(),
  sessionExpiresAt: new Date(Date.now() + 3_600_000).toISOString(), user: { id, role: "VIEWER" },
});

describe("AuthProvider", () => {
  beforeEach(() => {
    __resetForTests();
    created.length = 0;
  });

  it("creates a fresh Apollo client per identity and disposes it on logout/switch", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response("{}", { status: 401 })) // restore(): no session
      .mockResolvedValueOnce(new Response(body("alice"), { status: 200 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 })) // logout
      .mockResolvedValueOnce(new Response(body("bob"), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<AuthProvider><WhoAmI /></AuthProvider>);
    expect(await screen.findByText("anonymous:none")).toBeTruthy();

    await act(async () => void (await login("alice@pulseops.test", "pw")));
    expect(await screen.findByText("authenticated:alice")).toBeTruthy();
    const aliceClients = created.length;
    expect(aliceClients).toBeGreaterThan(0);

    await act(async () => void (await logout()));
    expect(await screen.findByText("anonymous:none")).toBeTruthy();
    created.slice(0, aliceClients).forEach((c) => expect(c.dispose).toHaveBeenCalled());

    await act(async () => void (await login("bob@pulseops.test", "pw")));
    expect(await screen.findByText("authenticated:bob")).toBeTruthy();
    expect(created.length).toBeGreaterThan(aliceClients); // Bob never reuses Alice's client/cache
    vi.unstubAllGlobals();
  });
});
