import { defineConfig, devices } from "@playwright/test";

// Runs against the Compose stack (./scripts/demo-up.sh). Traces, screenshots and video are
// disabled for every test so credentials and one-time ingest tokens are never recorded.
export default defineConfig({
  testDir: "./e2e",
  timeout: 300_000,
  expect: { timeout: 15_000 },
  workers: 1,
  retries: 0,
  reporter: [["list"], ["junit", { outputFile: "test-results/e2e-junit.xml" }]],
  use: {
    baseURL: process.env.PULSEOPS_BASE_URL ?? "http://127.0.0.1:8080",
    trace: "off",
    screenshot: "off",
    video: "off",
    ...devices["Desktop Chrome"],
  },
});
