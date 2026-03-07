import { defineConfig } from "@playwright/test";

const testUrl = process.env.TEST_URL;

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  retries: 0,
  use: {
    baseURL: testUrl || "http://localhost:8420",
    headless: true,
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
  // Only start Vite dev server when no TEST_URL is provided
  ...(testUrl
    ? {}
    : {
        webServer: {
          command: "npm run dev -- --port 5199",
          port: 5199,
          reuseExistingServer: true,
        },
      }),
});
