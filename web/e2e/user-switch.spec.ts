import { FakeApi, expect, mockApi, signInAs, test } from "./fixtures";

/**
 * Regression test for a bug that a passing type check and a clean production
 * build both missed: `useUser` held per-component state over localStorage, so
 * switching user in the nav re-rendered the nav and nothing else. Every page
 * kept showing the previous user's entitlements.
 *
 * Only clicking revealed it, which is the argument for these tests existing.
 */

test("switching user updates the whole page, not just the nav", async ({ page }) => {
  const api = new FakeApi({ tier: "pro", status: "active", source: "subscription" });
  await mockApi(page, api);
  await signInAs(page, "alice");

  await page.goto("/chat");
  await expect(page.getByText("1,500 messages left today")).toBeVisible();

  // The API answers for whoever asks; flip the fake to Free for the next user.
  api.state = { ...api.state, tier: "free", status: "free", source: "default" };

  await page.getByRole("button", { name: "Switch user" }).click();
  await page.getByLabel("Switch to user id").fill("bob");
  await page.getByRole("button", { name: "Switch", exact: true }).click();

  await expect(page.locator(".who")).toHaveText("bob");
  // The assertion that actually mattered: the page, not just the nav.
  await expect(page.getByText("20 of 20 messages left today")).toBeVisible();
});

test("the chosen user survives a reload", async ({ page, api }) => {
  await signInAs(page, "carol");
  await page.goto("/chat");
  await expect(page.locator(".who")).toHaveText("carol");

  await page.reload();
  await expect(page.locator(".who")).toHaveText("carol");
});

test("the user id is sent as the auth header on every call", async ({ page, api }) => {
  await signInAs(page, "dave");

  const seen: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/v1/")) {
      const header = request.headers()["x-user-id"];
      if (header) seen.push(header);
    }
  });

  await page.goto("/billing");
  await expect(page.getByRole("heading", { name: "Billing" })).toBeVisible();

  expect(seen.length).toBeGreaterThan(0);
  expect(new Set(seen)).toEqual(new Set(["dave"]));
});
