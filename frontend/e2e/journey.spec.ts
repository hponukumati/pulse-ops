import { expect, test } from "@playwright/test";

import { creds, syntheticEvents, uiLogin } from "./helpers";

/**
 * Product journey through the UI with synthetic data:
 * register route → issue token (reauth, one-time secret) → ingest → real metrics →
 * alert rule → incident opened by the worker → acknowledge → resolve.
 * Restart persistence is checked by scripts/e2e.sh around this spec.
 */
test("operator product journey", async ({ page, request }) => {
  const template = `/e2e/run${Date.now().toString(36).replace(/\d/g, "")}/{id}`;
  await uiLogin(page, "alice");
  await page.getByRole("link", { name: "checkout-api" }).click();

  // Register a route.
  await page.getByRole("tab", { name: "Settings" }).click();
  await page.getByLabel("Route template").fill(template);
  await page.getByRole("button", { name: "Register route" }).click();
  const row = page.getByRole("row").filter({ hasText: template });
  await expect(row).toBeVisible();
  const routeId = (await row.locator("td").nth(1).innerText()).trim();

  // Issue an ingest token: password confirmation, then a one-time secret dialog.
  await page.getByRole("button", { name: "Create token" }).click();
  await page.getByLabel("Password", { exact: true }).fill(creds.accounts.alice.password);
  await page.getByRole("button", { name: "Confirm" }).click();
  const secretBox = page.getByTestId("ingest-secret");
  await expect(secretBox).toContainText("poi_");
  const secret = (await secretBox.innerText()).trim();
  await page.getByRole("button", { name: "I have stored it" }).click();
  await expect(secretBox).toHaveCount(0); // gone from the DOM once closed

  // Ingest synthetic events (60% 5xx) with the service-scoped credential.
  const ingest = await request.post("/api/v1/ingest/events", {
    headers: { Authorization: `Bearer ${secret}`, "Content-Type": "application/json" },
    data: syntheticEvents(routeId, 30, 0.6),
  });
  expect(ingest.status()).toBe(200);
  expect((await ingest.json()).accepted).toBe(30);

  // Metrics reflect the stored events within the polling interval (route filter = this route).
  await page.getByRole("tab", { name: "Metrics" }).click();
  await page.getByLabel("Route").selectOption({ label: template });
  await expect(page.locator(".kpi").filter({ hasText: "Requests" }).first()).toContainText("30", { timeout: 15_000 });
  await expect(page.locator(".kpi").filter({ hasText: "5xx error rate" })).toContainText("60.0%");

  // Alert rule: 5xx rate > 10% over 5 minutes, min 5 samples.
  await page.getByRole("tab", { name: "Rules" }).click();
  await page.getByRole("button", { name: "New rule" }).click();
  await page.getByLabel("Name").fill("e2e 5xx");
  await page.getByLabel(/Threshold/).fill("0.1");
  await page.getByLabel("Minimum samples").fill("5");
  await page.getByRole("button", { name: "Save rule" }).click();
  await expect(page.getByRole("cell", { name: /e2e 5xx/ })).toBeVisible();

  // The dedicated worker needs two consecutive breaching 60 s slots (window ends 60 s before the slot).
  await page.getByRole("tab", { name: "Incidents" }).click();
  const incident = page.getByRole("row").filter({ hasText: "e2e 5xx" });
  await expect(incident.getByText("Open")).toBeVisible({ timeout: 240_000 });
  await incident.getByRole("button", { name: "Acknowledge" }).click();
  await expect(incident.getByText("Acknowledged")).toBeVisible();
  await incident.getByRole("button", { name: "Resolve" }).click();
  await page.getByLabel("Show active only").uncheck();
  await expect(page.getByRole("row").filter({ hasText: "e2e 5xx" }).first().getByText("Resolved")).toBeVisible();

  // Clean up the rule so later runs start fresh.
  await page.getByRole("tab", { name: "Rules" }).click();
  await page.getByRole("row").filter({ hasText: "e2e 5xx" }).getByRole("button", { name: "Delete" }).click();
});
