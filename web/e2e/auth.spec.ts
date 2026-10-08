import { PRO_SUBSCRIBER, TEST_EMAIL, expect, planCard, signIn, test } from "./fixtures";

/**
 * Sessions, and what is reachable without one.
 *
 * This replaced a suite built around a localStorage user-switcher, which stood
 * in for auth before there was any. The property worth keeping from it: a
 * change of identity must reach every component, not just the nav.
 */

test("a signed-out visitor is sent to sign in, and back again after", async ({ page }) => {
  // /chat rather than /billing: billing is where signing in lands when `next`
  // is missing, so it could not show that `next` was followed.
  await page.goto("/chat");

  await expect(page).toHaveURL(/\/login/);
  expect(new URL(page.url()).searchParams.get("next")).toBe("/chat");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();

  await signIn(page, { loginUrl: page.url() });

  expect(new URL(page.url()).pathname).toBe("/chat");
  await expect(page.getByRole("heading", { name: "Chat" })).toBeVisible();
});

test("the pricing page renders without a session", async ({ page, api }) => {
  // It is the pricing page: someone who has never signed up has to see it.
  await page.goto("/");

  await expect(planCard(page, "Pro")).toContainText("$100/mo");
  await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
  // Nothing is marked as theirs, because there is no "them" yet.
  await expect(page.locator(".plan .tag", { hasText: "Current plan" })).toHaveCount(0);
});

test("upgrading while signed out asks you to sign in first", async ({ page, api }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Upgrade to Pro" }).click();

  await expect(page).toHaveURL(/\/login/);
  expect(api.checkoutBodies).toEqual([]);
});

test("signing in reaches every component, not just the nav", async ({ page, api }) => {
  Object.assign(api.state, PRO_SUBSCRIBER);
  await signIn(page);

  await expect(page.locator(".who")).toHaveText(TEST_EMAIL);

  await page.goto("/chat");
  await expect(page.getByText("1,500 messages left today")).toBeVisible();
});

test("every API call carries the bearer token", async ({ page, api }) => {
  await signIn(page);

  const authHeaders: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/v1/entitlements")) {
      authHeaders.push(request.headers()["authorization"] ?? "(none)");
    }
  });

  await page.goto("/billing");
  await expect(page.getByRole("heading", { name: "Billing" })).toBeVisible();

  expect(authHeaders.length).toBeGreaterThan(0);
  for (const header of authHeaders) {
    expect(header).toMatch(/^Bearer .+\..+\..+$/);
  }
});

test("signing out returns you to the signed-out state", async ({ page, api }) => {
  await signIn(page);
  await page.goto("/billing");
  await expect(page.locator(".who")).toHaveText(TEST_EMAIL);

  await page.getByRole("button", { name: "Sign out" }).click();

  await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
  await expect(page.locator(".who")).toHaveCount(0);
});

test("the session survives a reload", async ({ page, api }) => {
  await signIn(page);
  await page.goto("/billing");
  await expect(page.locator(".who")).toHaveText(TEST_EMAIL);

  await page.reload();
  await expect(page.locator(".who")).toHaveText(TEST_EMAIL);
});
