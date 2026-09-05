import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // Vitest's default `include` also matches e2e/*.spec.ts, which are
    // Playwright tests and would fail with confusing errors about a missing
    // browser. Unit tests live next to the code they cover.
    include: ["lib/**/*.test.ts", "components/**/*.test.ts"],
    exclude: ["e2e/**", "node_modules/**", ".next/**"],
    environment: "node",

    coverage: {
      provider: "v8",
      // Only what these tests are meant to cover. Pages and components are
      // exercised by Playwright, which measures behaviour rather than lines;
      // counting them here would report a number that means nothing.
      include: ["lib/**/*.ts"],
      exclude: ["lib/**/*.test.ts", "lib/supabase.ts", "lib/user.ts", "lib/api.ts"],
      reporter: ["text", "json-summary"],
      reportsDirectory: "coverage",
      // Floors, not targets -- see the same reasoning in pyproject.toml.
      // These functions are pure and cheap to cover, so the bar is high.
      thresholds: { statements: 95, branches: 90, functions: 100, lines: 95 },
    },
  },
});
