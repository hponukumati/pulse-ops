import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";

import { expect, type APIRequestContext, type Page } from "@playwright/test";

type Account = { email: string; password: string; role: string; id: string };
type Creds = { accounts: Record<"admin" | "alice" | "bob" | "victor", Account>; services: Record<string, string> };

export const creds: Creds = JSON.parse(readFileSync(process.env.PULSEOPS_DEMO_CREDENTIALS ?? "../.demo-credentials.json", "utf8"));
export const BASE = process.env.PULSEOPS_BASE_URL ?? "http://127.0.0.1:8080";
export const AUTH = { Origin: BASE, "X-PulseOps-CSRF": "1", "Content-Type": "application/json" };

export async function uiLogin(page: Page, who: keyof Creds["accounts"]): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill(creds.accounts[who].email);
  await page.getByLabel("Password", { exact: true }).fill(creds.accounts[who].password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Services" })).toBeVisible();
}

export async function apiToken(request: APIRequestContext, who: keyof Creds["accounts"]): Promise<string> {
  const r = await request.post("/api/v1/auth/login", { headers: AUTH, data: { email: creds.accounts[who].email, password: creds.accounts[who].password } });
  expect(r.status()).toBe(200);
  return (await r.json()).accessToken as string;
}

export async function gql(request: APIRequestContext, token: string, query: string, variables: Record<string, unknown> = {}) {
  const r = await request.post("/graphql", { headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }, data: { query, variables } });
  return r.json();
}

export function syntheticEvents(routeId: string, n: number, errorShare: number) {
  const at = new Date(Date.now() - 20_000).toISOString();
  return Array.from({ length: n }, (_, i) => ({
    eventId: randomUUID(),
    timestamp: at,
    method: "GET",
    routeId,
    statusCode: i < Math.round(n * errorShare) ? 503 : 200,
    durationMs: 20 + i,
  }));
}
