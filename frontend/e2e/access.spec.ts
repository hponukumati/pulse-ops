import { expect, test } from "@playwright/test";

import { apiToken, creds, gql, uiLogin } from "./helpers";

test("viewer cannot mutate in the UI or via direct API calls", async ({ page, request }) => {
  await uiLogin(page, "victor");
  await page.getByRole("link", { name: "checkout-api" }).click();
  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(page.getByRole("button", { name: "Register route" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Create token" })).toHaveCount(0);
  await page.getByRole("tab", { name: "Rules" }).click();
  await expect(page.getByRole("button", { name: "New rule" })).toHaveCount(0);

  const token = await apiToken(request, "victor");
  const sid = creds.services["checkout-api"];
  for (const [q, v] of [
    ['mutation($s: ID!) { registerRoute(serviceId: $s, template: "/viewer") { id } }', { s: sid }],
    ['mutation($s: ID!) { updateService(id: $s, input: {name: "renamed"}) { id } }', { s: sid }],
    ["mutation($s: ID!) { createIngestToken(serviceId: $s) { secret } }", { s: sid }],
  ] as const) {
    const body = await gql(request, token, q, v);
    expect(body.errors[0].extensions.code).toBe("FORBIDDEN");
  }
});

test("logout and identity switch never show the previous user's data", async ({ page }) => {
  await uiLogin(page, "alice");
  await expect(page.getByRole("link", { name: "checkout-api" })).toBeVisible();
  const aliceService = `/services/${creds.services["checkout-api"]}`;
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in to PulseOps" })).toBeVisible();

  // Nothing sensitive persisted in the browser.
  const stored = await page.evaluate(async () => ({
    local: { ...localStorage },
    session: { ...sessionStorage },
    cookie: document.cookie,
    idb: "databases" in indexedDB ? (await indexedDB.databases()).map((d) => d.name) : [],
  }));
  expect(stored).toEqual({ local: {}, session: {}, cookie: "", idb: [] });

  await uiLogin(page, "bob");
  await expect(page.getByRole("link", { name: "billing-api" })).toBeVisible();
  await expect(page.getByText("checkout-api")).toHaveCount(0);
  await page.goto(aliceService);
  await expect(page.getByText("Not found, or you do not have access.")).toBeVisible();
});

test("access token never reaches browser storage and refresh cookie is HttpOnly", async ({ page, context }) => {
  await uiLogin(page, "alice");
  const stored = await page.evaluate(() => ({ local: JSON.stringify({ ...localStorage }), session: JSON.stringify({ ...sessionStorage }), cookie: document.cookie }));
  expect(stored.local).toBe("{}");
  expect(stored.session).toBe("{}");
  expect(stored.cookie).not.toContain("pulseops_refresh");
  const cookies = await context.cookies();
  const refresh = cookies.find((c) => c.name.includes("pulseops_refresh"));
  expect(refresh?.httpOnly).toBe(true);
  expect(refresh?.sameSite).toBe("Strict");
  // Reload restores the session from the HttpOnly cookie without any stored token.
  await page.reload();
  await expect(page.getByRole("heading", { name: "Services" })).toBeVisible();
});

test("mobile layout at 375px has no horizontal scroll", async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 375, height: 812 } });
  const page = await ctx.newPage();
  await uiLogin(page, "alice");
  await page.getByRole("link", { name: "checkout-api" }).click();
  await expect(page.locator(".kpi").first()).toBeVisible();
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(width).toBeLessThanOrEqual(375);
  await ctx.close();
});
