import type { CheckoutPayload, PortalPayload } from "../lib/types";
import { PLUS_SUBSCRIBER, PRO_SUBSCRIBER, expect, planCard, signIn, test } from "./fixtures";

test.beforeEach(async ({ page }) => {
  await signIn(page);
});

test("renders limits from config and amounts from Stripe", async ({ page }) => {
  await page.goto("/");

  const pro = planCard(page, "Pro");
  await expect(pro).toContainText("$100/mo");
  await expect(pro).toContainText("1,500");
  await expect(pro).toContainText("reasoning");

  const free = planCard(page, "Free");
  await expect(free).toContainText("20");
});

test("the annual toggle swaps the amounts", async ({ page }) => {
  await page.goto("/");
  const plus = planCard(page, "Plus");
  await expect(plus).toContainText("$20/mo");

  await page.getByRole("button", { name: "Annual" }).click();
  await expect(plus).toContainText("$200/yr");
});

test("a free user checks out the plan on the card, monthly by default", async ({ page, api }) => {
  await page.goto("/");
  await planCard(page, "Pro").getByRole("button", { name: "Upgrade to Pro" }).click();

  await expect
    .poll(() => api.checkoutBodies)
    .toEqual([{ tier: "pro", interval: "monthly" }] satisfies CheckoutPayload[]);
});

test("the annual toggle travels to checkout", async ({ page, api }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Annual" }).click();
  await planCard(page, "Plus").getByRole("button", { name: "Upgrade to Plus" }).click();

  await expect
    .poll(() => api.checkoutBodies)
    .toEqual([{ tier: "plus", interval: "annual" }] satisfies CheckoutPayload[]);
});

test("a subscriber is sent to the portal, never to a second checkout", async ({ page, api }) => {
  // The API refuses a second checkout with a 409; the UI should not offer one.
  Object.assign(api.state, PLUS_SUBSCRIBER);
  await page.goto("/");

  // Labelled by what happens to *their* plan. "Change in portal" named the
  // mechanism and left the customer to work out which direction they were going.
  const pro = planCard(page, "Pro");
  await expect(pro.getByRole("button", { name: "Upgrade to Pro" })).toBeVisible();
  await expect(
    planCard(page, "Free").getByRole("link", { name: "Cancel subscription" }),
  ).toBeVisible();

  await pro.getByRole("button", { name: "Upgrade to Pro" }).click();
  // The tier travels with the click, so Stripe opens on a confirmation for Pro
  // rather than on a list the customer has to search.
  await expect
    .poll(() => api.portalBodies)
    .toEqual([{ tier: "pro", interval: "monthly" }] satisfies PortalPayload[]);
  expect(api.checkoutBodies).toEqual([]);
});

test("the current plan is marked and cannot be re-bought", async ({ page, api }) => {
  Object.assign(api.state, PRO_SUBSCRIBER);
  await page.goto("/");

  const pro = planCard(page, "Pro");
  // The eyebrow is uppercased in CSS, so the DOM text is still sentence case.
  await expect(pro.locator(".tag")).toHaveText("Current plan");
  await expect(pro.getByRole("button", { name: "Current plan" })).toBeDisabled();
});

test("a Stripe outage leaves the page usable without amounts", async ({ page, api }) => {
  // The API degrades to null amounts rather than failing; the page should too.
  await page.route("**/api/v1/billing/plans", async (route) => {
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
  const pro = planCard(page, "Pro");
  await expect(pro).toContainText("—");
  await expect(pro).toContainText("1,500");
});

test("the annual toggle travels to the portal too", async ({ page, api }) => {
  Object.assign(api.state, PLUS_SUBSCRIBER);
  await page.goto("/");

  await page.getByRole("button", { name: "Annual" }).click();
  await planCard(page, "Pro").getByRole("button", { name: "Upgrade to Pro" }).click();

  await expect
    .poll(() => api.portalBodies)
    .toEqual([{ tier: "pro", interval: "annual" }] satisfies PortalPayload[]);
});

test("Manage billing asks for no particular plan", async ({ page, api }) => {
  // The billing page's button opens the portal itself, not a plan change.
  Object.assign(api.state, PRO_SUBSCRIBER);
  await page.goto("/billing");

  await page.getByRole("button", { name: "Manage billing" }).click();

  await expect.poll(() => api.portalBodies).toEqual([{}] satisfies PortalPayload[]);
});
