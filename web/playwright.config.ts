import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? "github" : [["list"]],

  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
    // The app formats dates with toLocaleDateString, so without pinning these
    // every date assertion would depend on the machine running the tests --
    // "Sep 10 UTC" renders as "Sep 9" in Pacific.
    locale: "en-US",
    timezoneId: "UTC",
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],

  // These tests mock /api/**, so only the Next server is needed -- no Postgres,
  // no Redis, no Stripe, no uvicorn. Reuses a dev server if one is already up.
  webServer: {
    // CI has already run `next build`, so test the production bundle there --
    // dev mode compiles on demand and is not what ships. Locally, reuse
    // whatever dev server is already running.
    command: process.env.CI ? "npm run start" : "npm run dev",
    url: "http://localhost:3000",
    // The app refuses to start without these. Real values are irrelevant here:
    // every Supabase endpoint is intercepted, so these only need to exist.
    env: {
      NEXT_PUBLIC_SUPABASE_URL:
        process.env.NEXT_PUBLIC_SUPABASE_URL ?? "https://test-project.supabase.co",
      NEXT_PUBLIC_SUPABASE_ANON_KEY:
        process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "test-anon-key",
    },
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
