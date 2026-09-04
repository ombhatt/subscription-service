import { FakeApi, expect, mockApi, signIn, test } from "./fixtures";

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test("renders limits from config and amounts from Stripe", async ({ page }) => {
  await page.goto("/");

  const pro = page.locator(".plan", { hasText: "Pro" });
  await expect(pro).toContainText("$100/mo");
  await expect(pro).toContainText("1,500");
  await expect(pro).toContainText("reasoning");

  const free = page.locator(".plan", { hasText: "Free" }).first();
  await expect(free).toContainText("20");
});

test("the annual toggle swaps the amounts", async ({ page }) => {
  await page.goto("/");
  const plus = page.locator(".plan", { hasText: "Plus" });
  await expect(plus).toContainText("$20/mo");

  await page.getByRole("button", { name: "Annual" }).click();
  await expect(plus).toContainText("$200/yr");
});

test("a free user can start checkout", async ({ page, api }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Upgrade to Pro" }).click();
  await expect.poll(() => api.checkoutCalls).toBe(1);
});

test("a subscriber is sent to the portal, never to a second checkout", async ({ page }) => {
  // The API refuses a second checkout with a 409; the UI should not offer one.
  const api = new FakeApi({ tier: "plus", status: "active", source: "subscription" });
  await mockApi(page, api);
  await page.goto("/");

  await expect(page.getByRole("button", { name: /Upgrade to/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Change in portal" })).toHaveCount(2);

  await page.locator(".plan", { hasText: "Pro" }).getByRole("button").click();
  await expect.poll(() => api.portalCalls).toBe(1);
});

test("the current plan is marked and cannot be re-bought", async ({ page }) => {
  const api = new FakeApi({ tier: "pro", status: "active", source: "subscription" });
  await mockApi(page, api);
  await page.goto("/");

  const pro = page.locator(".plan", { hasText: "Pro" });
  // The eyebrow is uppercased in CSS, so the DOM text is still sentence case.
  await expect(pro.locator(".tag")).toHaveText("Current plan");
  await expect(pro.getByRole("button", { name: "Current plan" })).toBeDisabled();
});

test("a Stripe outage leaves the page usable without amounts", async ({ page }) => {
  // The API degrades to null amounts rather than failing; the page should too.
  await page.route("**/api/v1/billing/plans", async (route) => {
    const api = new FakeApi();
    const plans = api.plans().map((plan) => ({
      ...plan,
      prices: Object.fromEntries(
        Object.entries(plan.prices).map(([key, price]) => [
          key,
          { ...price, unit_amount: null, currency: null },
        ]),
      ),
    }));
    await route.fulfill({ json: plans });
  });

  await page.goto("/");
  const pro = page.locator(".plan", { hasText: "Pro" });
  await expect(pro).toContainText("—");
  await expect(pro).toContainText("1,500");
});
