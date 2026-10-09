import { defineConfig, devices } from "@playwright/test";

// Overridable so parallel worktrees on one machine cannot fight over 4173.
const PORT = Number(process.env.PLAYWRIGHT_PORT ?? 4173);

const smoke = [
  { name: "smoke-mobile-portrait", ...devices["Pixel 7"] },
  { name: "smoke-tablet-landscape", ...devices["iPad Mini landscape"] },
  { name: "smoke-desktop", ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
] as const;

const full = [
  { name: "full-mobile-320-portrait", ...devices["Desktop Chrome"], viewport: { width: 320, height: 844 } },
  { name: "full-mobile-landscape", ...devices["Desktop Chrome"], viewport: { width: 844, height: 320 } },
  { name: "full-tablet-768-portrait", ...devices["Desktop Chrome"], viewport: { width: 768, height: 1024 } },
  { name: "full-tablet-768-landscape", ...devices["Desktop Chrome"], viewport: { width: 1024, height: 768 } },
  { name: "full-tablet-1024-portrait", ...devices["Desktop Chrome"], viewport: { width: 1024, height: 1366 } },
  { name: "full-desktop-1440", ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
] as const;

export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  timeout: 30_000,
  retries: process.env.CI ? 1 : 0,
  outputDir: "test-results",
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    video: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `npm run preview -- --port ${PORT} --strictPort --host 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
  projects: [...smoke, ...full],
});
