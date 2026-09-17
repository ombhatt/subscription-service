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
  // Undoing it is a control in the banner, not an instruction to go and find
  // Stripe's portal.
  await expect(banner.getByRole("button", { name: "Resume subscription" })).toBeVisible();
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


test("a redeemed promotion code is shown, with its size and end date", async ({ page }) => {
  const api = new FakeApi({
    tier: "pro",
    status: "active",
    source: "subscription",
    discount: {
      coupon_id: "LAUNCH25",
      name: "Launch 25",
      percent_off: 25,
      amount_off: null,
      currency: null,
      duration: "repeating",
      duration_in_months: 3,
      promotion_code: "promo_1",
      // 2027-01-04T00:00:00Z
      ends_at: 1799020800,
    },
  });
  await mockApi(page, api);
  await page.goto("/billing");

  const banner = page.locator(".banner", { hasText: "Discount applied" });
  await expect(banner).toContainText("25% off for 3 months");
  await expect(banner).toContainText("Jan 4, 2027");
  await expect(banner).toContainText("promotion code");
});


test("no discount means no banner", async ({ page, api }) => {
  await page.goto("/billing");
  await expect(page.locator(".banner", { hasText: "Discount applied" })).toHaveCount(0);
});

test("cancelling in the app schedules the end of the period", async ({ page }) => {
  const api = new FakeApi({
    tier: "pro",
    status: "active",
    source: "subscription",
    currentPeriodEnd: "2026-10-03T00:00:00Z",
  });
  await mockApi(page, api);
  await page.goto("/billing");
  await expect(page.getByText(/Renews Oct 3, 2026/)).toBeVisible();

  await page.getByRole("button", { name: "Cancel subscription" }).click();
  await page.getByRole("button", { name: /Yes, cancel/ }).click();

  await expect(page.locator(".banner.warn")).toContainText("Subscription ending");
  // The card must agree with the banner: it no longer renews.
  await expect(page.getByText(/Ends Oct 3, 2026/)).toBeVisible();
  await expect(page.getByText(/Renews/)).toHaveCount(0);
  expect(api.cancelCalls).toBe(1);
});

test("the confirmation step can be backed out of", async ({ page }) => {
  const api = new FakeApi({
    tier: "pro",
    status: "active",
    source: "subscription",
    currentPeriodEnd: "2026-10-03T00:00:00Z",
  });
  await mockApi(page, api);
  await page.goto("/billing");

  await page.getByRole("button", { name: "Cancel subscription" }).click();
  await expect(page.getByText(/Cancel your Pro plan\?/)).toBeVisible();
  await page.getByRole("button", { name: "Keep my plan" }).click();

  await expect(page.getByText(/Cancel your Pro plan\?/)).toHaveCount(0);
  await expect(page.getByText(/Renews Oct 3, 2026/)).toBeVisible();
  expect(api.cancelCalls).toBe(0);
});

test("a plan already ending offers no cancel button", async ({ page }) => {
  const api = new FakeApi({
    tier: "pro",
    status: "active",
    source: "subscription",
    cancelAtPeriodEnd: true,
    currentPeriodEnd: "2026-10-03T00:00:00Z",
  });
  await mockApi(page, api);
  await page.goto("/billing");

  await expect(page.getByRole("button", { name: "Cancel subscription" })).toHaveCount(0);
});

test("the plan card says what it costs", async ({ page }) => {
  // The one number a customer opens this page to check, and it was not here.
  const api = new FakeApi({
    tier: "pro",
    status: "active",
    source: "subscription",
    currentPeriodEnd: "2026-10-03T00:00:00Z",
  });
  await mockApi(page, api);
  await page.goto("/billing");

  await expect(page.locator(".card").first().locator(".price-now")).toHaveText("$100/mo");
});

test("a discount is applied to the amount shown, not just described", async ({ page }) => {
  const api = new FakeApi({
    tier: "pro",
    status: "active",
    source: "subscription",
    currentPeriodEnd: "2026-10-03T00:00:00Z",
    discount: {
      coupon_id: "LAUNCH",
      name: "Launch",
      percent_off: 25,
      amount_off: null,
      currency: null,
      duration: "forever",
      duration_in_months: null,
      promotion_code: "LAUNCH25",
      ends_at: null,
    },
  });
  await mockApi(page, api);
  await page.goto("/billing");

  const card = page.locator(".card").first();
  await expect(card.locator(".price-now")).toHaveText("$75/mo");
  await expect(card.locator(".price-was")).toHaveText("$100");
});

test("a pending cancellation can be undone from the banner", async ({ page }) => {
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
  // The old copy said "you can reactivate in the portal" -- an instruction, not
  // a control.
  await expect(banner).not.toContainText("in the portal");

  await banner.getByRole("button", { name: "Resume subscription" }).click();

  await expect(page.locator(".banner.warn")).toHaveCount(0);
  await expect(page.getByText(/Renews Oct 3, 2026/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Cancel subscription" })).toBeVisible();
  expect(api.resumeCalls).toBe(1);
});
