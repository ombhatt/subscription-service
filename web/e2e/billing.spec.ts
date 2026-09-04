import { FakeApi, expect, mockApi, signIn, test } from "./fixtures";

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test("shows the plan, its renewal date and its usage", async ({ page }) => {
  const api = new FakeApi({
    tier: "pro",
    status: "active",
    source: "subscription",
    messagesUsed: 12,
    currentPeriodEnd: "2026-10-03T03:43:47Z",
  });
  await mockApi(page, api);
  await page.goto("/billing");

  await expect(page.getByRole("heading", { name: /Pro/ })).toBeVisible();
  await expect(page.locator(".pill")).toHaveText("active");
  await expect(page.getByText(/Renews Oct 3, 2026/)).toBeVisible();
  await expect(page.getByText("12 / 1,500")).toBeVisible();
});

test("an unlimited quota draws no bar", async ({ page }) => {
  // A full-width meter would imply a ceiling that does not exist.
  const api = new FakeApi({ tier: "pro", status: "active", source: "subscription" });
  await mockApi(page, api);
  await page.goto("/billing");

  // Scoped to the meter: "Unlimited" also appears in the feature list below.
  await expect(page.locator(".meter-label .value", { hasText: "unlimited" })).toBeVisible();
  await expect(page.getByText("No cap on this plan")).toBeVisible();
  // Two quotas, but only the capped one gets a meter.
  await expect(page.locator(".meter")).toHaveCount(1);
});

test("a failed payment explains the grace window", async ({ page }) => {
  const api = new FakeApi({
    tier: "plus",
    status: "past_due",
    source: "subscription",
    graceEndsAt: "2026-09-10T00:00:00Z",
    currentPeriodEnd: "2026-10-03T00:00:00Z",
  });
  await mockApi(page, api);
  await page.goto("/billing");

  const banner = page.locator(".banner.warn");
  await expect(banner).toContainText("Your last payment failed");
  await expect(banner).toContainText("Sep 10, 2026");
  // Still Plus: the point of the grace window.
  await expect(page.getByRole("heading", { name: /Plus/ })).toBeVisible();
});

test("a cancelled subscription says when access ends", async ({ page }) => {
  const api = new FakeApi({
    tier: "pro",
    status: "active",
    source: "subscription",
    cancelAtPeriodEnd: true,
    currentPeriodEnd: "2026-10-03T00:00:00Z",
  });
  await mockApi(page, api);
  await page.goto("/billing");

  const banner = page.locator(".banner.warn");
  await expect(banner).toContainText("Subscription ending");
  await expect(banner).toContainText("Oct 3, 2026");
  await expect(banner).toContainText("reactivate");
});

test("a comped account is labelled as granted", async ({ page }) => {
  const api = new FakeApi({ tier: "pro", status: "free", source: "grant" });
  await mockApi(page, api);
  await page.goto("/billing");

  await expect(page.locator(".banner")).toContainText("Complimentary access");
  await expect(page.getByText("Granted plan")).toBeVisible();
});

test("a free user with no billing account is told, not broken", async ({ page }) => {
  await page.route("**/api/v1/billing/portal", async (route) => {
    await route.fulfill({
      status: 404,
      json: { detail: "no billing account yet -- subscribe first" },
    });
  });

  await page.goto("/billing");
  await page.getByRole("button", { name: "Manage billing" }).click();

  await expect(page.locator(".banner.error")).toContainText("no billing account yet");
});
