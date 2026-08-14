import { defineConfig } from "@playwright/test";

const port = 4175;
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: "./e2e",
  outputDir: "../../test-results/playwright",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  reporter: "line",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  webServer: {
    command:
      "corepack pnpm build && corepack pnpm exec vite preview --host 127.0.0.1 --port 4175 --strictPort",
    url: baseURL,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
