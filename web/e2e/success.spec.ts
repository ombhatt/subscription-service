import { FakeApi, expect, mockApi, signIn, test } from "./fixtures";

/**
 * The success page is the one surface where the architecture is visible to the
 * customer: it grants nothing, because the webhook does. So it polls, and it
 * has to be honest when the webhook does not arrive.
 */

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test("waits for the webhook, then confirms the plan", async ({ page }) => {
  const api = new FakeApi({ grantAfterReads: { reads: 3, tier: "pro" } });
  await mockApi(page, api);

  await page.goto("/billing/success");

  // Payment is done, but our service has not been told yet.
  await expect(page.getByRole("heading", { name: "Confirming your payment" })).toBeVisible();
  await expect(page.getByText(/You can safely leave this page/)).toBeVisible();

  // The webhook lands on the third read.
  await expect(page.getByRole("heading", { name: "You're on Pro" })).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByRole("link", { name: "View billing" })).toBeVisible();
});

test("says access came from the webhook, not from this page", async ({ page }) => {
  const api = new FakeApi({ grantAfterReads: { reads: 1, tier: "pro" } });
  await mockApi(page, api);

  await page.goto("/billing/success");
  await expect(page.getByText(/Access was granted by Stripe's webhook/)).toBeVisible({
    timeout: 15_000,
  });
});

test("a webhook that never arrives is explained, not hidden", async ({ page }) => {
  // Entitlements stay free forever: `stripe listen` is not running.
  const api = new FakeApi();
  await mockApi(page, api);

  await page.goto("/billing/success");

  // 15 attempts at ~1s, so give this one room.
  await expect(page.getByText("Still waiting on the webhook")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/stripe listen/)).toBeVisible();
  await expect(page.getByText(/The payment is not lost/)).toBeVisible();
});
