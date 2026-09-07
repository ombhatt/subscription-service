/**
 * Enterprise on the pricing page, and the contact-sales path that sells it.
 *
 * Enterprise has no price and no checkout, so the conversion event here is
 * someone leaving an address. These assert the two things that make that work:
 * that the card reads as sales-led rather than as a broken paid plan, and that
 * submitting actually reaches the API with what the backend needs.
 */

import AxeBuilder from "@axe-core/playwright";

import { FakeApi, expect, mockApi, signIn, test } from "./fixtures";

test.describe("the Enterprise card", () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page, new FakeApi());
    await page.goto("/");
  });

  test("shows a custom price, not a free one", async ({ page }) => {
    // Free and Enterprise are both unpurchasable; pricing Enterprise at zero
    // would be the single worst thing this page could say.
    const card = page.locator(".card.plan").filter({ hasText: "Enterprise" });
    await expect(card).toBeVisible();
    await expect(card.locator(".price-now")).toHaveText("Custom");
  });

  test("does not render unlimited context as zero", async ({ page }) => {
    const card = page.locator(".card.plan").filter({ hasText: "Enterprise" });
    await expect(card).toContainText("Unlimited");
    await expect(card).not.toContainText("0 context");
  });

  test("offers a conversation instead of a checkout", async ({ page }) => {
    const card = page.locator(".card.plan").filter({ hasText: "Enterprise" });
    await expect(card.getByRole("button", { name: "Contact sales" })).toBeVisible();
    await expect(card.getByRole("button", { name: /Upgrade to/ })).toHaveCount(0);
  });

  test("the free plan still reads as free", async ({ page }) => {
    const card = page.locator(".card.plan").filter({ hasText: "Free" }).first();
    await expect(card.locator(".price-now")).toHaveText("Free");
  });
});

test.describe("contacting sales", () => {
  test("an anonymous visitor can submit without signing up", async ({ page }) => {
    // The whole reason the endpoint is public: this person has no account yet.
    const api = new FakeApi();
    await mockApi(page, api);

    let sent: Record<string, unknown> | null = null;
    await page.route("**/api/v1/billing/contact-sales", async (route) => {
      sent = route.request().postDataJSON();
      await route.fulfill({ status: 201, json: { id: "inq_1", status: "received" } });
    });

    await page.goto("/");
    await page.getByRole("button", { name: "Contact sales" }).click();

    await page.getByLabel("Work email").fill("cto@acme.com");
    await page.getByLabel("Company (optional)").fill("Acme");
    await page.getByLabel("How many people (optional)").fill("250");
    await page.getByLabel("Anything we should know (optional)").fill("need SSO");
    await page.getByRole("button", { name: "Send request" }).click();

    await expect(page.getByText(/we'll be in touch/i)).toBeVisible();
    expect(sent).toMatchObject({
      email: "cto@acme.com",
      company: "Acme",
      seats: 250,
      message: "need SSO",
      source: "pricing_page",
    });
  });

  test("optional fields are genuinely optional", async ({ page }) => {
    let sent: Record<string, unknown> | null = null;
    await mockApi(page, new FakeApi());
    await page.route("**/api/v1/billing/contact-sales", async (route) => {
      sent = route.request().postDataJSON();
      await route.fulfill({ status: 201, json: { id: "inq_2", status: "received" } });
    });

    await page.goto("/");
    await page.getByRole("button", { name: "Contact sales" }).click();
    await page.getByLabel("Work email").fill("solo@acme.com");
    await page.getByRole("button", { name: "Send request" }).click();

    await expect(page.getByText(/we'll be in touch/i)).toBeVisible();
    expect(sent).toMatchObject({ email: "solo@acme.com", company: null, seats: null });
  });

  test("submit is unavailable until there is an address to reply to", async ({ page }) => {
    await mockApi(page, new FakeApi());
    await page.goto("/");
    await page.getByRole("button", { name: "Contact sales" }).click();
    await expect(page.getByRole("button", { name: "Send request" })).toBeDisabled();
  });

  test("a signed-in subscriber sends their token so the lead is attributed", async ({ page }) => {
    // A Pro subscriber asking about Enterprise is a different conversation.
    await signIn(page);
    await mockApi(page, new FakeApi({ tier: "pro", status: "active", source: "subscription" }));

    let auth: string | undefined;
    await page.route("**/api/v1/billing/contact-sales", async (route) => {
      auth = route.request().headers()["authorization"];
      await route.fulfill({ status: 201, json: { id: "inq_3", status: "received" } });
    });

    await page.goto("/");
    await page.getByRole("button", { name: "Contact sales" }).click();
    await page.getByLabel("Work email").fill("alice@acme.com");
    await page.getByRole("button", { name: "Send request" }).click();

    await expect(page.getByText(/we'll be in touch/i)).toBeVisible();
    expect(auth ?? "").toMatch(/^Bearer /);
  });

  test("a failure is announced, not swallowed", async ({ page }) => {
    await mockApi(page, new FakeApi());
    await page.route("**/api/v1/billing/contact-sales", (route) =>
      route.fulfill({ status: 500, json: { detail: "database is on fire" } }),
    );

    await page.goto("/");
    await page.getByRole("button", { name: "Contact sales" }).click();
    await page.getByLabel("Work email").fill("cto@acme.com");
    await page.getByRole("button", { name: "Send request" }).click();

    const alert = page.getByRole("alert").filter({ hasText: /didn't send/i });
    await expect(alert).toBeVisible();
    // Still fillable, so the lead is not lost to a transient failure.
    await expect(page.getByLabel("Work email")).toHaveValue("cto@acme.com");
  });

  test("the form has no accessibility violations", async ({ page }) => {
    await mockApi(page, new FakeApi());
    await page.goto("/");
    await page.getByRole("button", { name: "Contact sales" }).click();
    await expect(page.getByLabel("Work email")).toBeVisible();

    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });
});
