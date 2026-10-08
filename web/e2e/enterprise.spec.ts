/**
 * Enterprise on the pricing page, and the contact-sales path that sells it.
 *
 * Enterprise has no price and no checkout, so the conversion event here is
 * someone leaving an address. These assert the two things that make that work:
 * that the card reads as sales-led rather than as a broken paid plan, and that
 * submitting actually reaches the API with what the backend needs.
 */

import AxeBuilder from "@axe-core/playwright";

import { PRO_SUBSCRIBER, expect, planCard, signIn, test } from "./fixtures";

test.describe("the Enterprise card", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
  });

  test("shows a custom price, not a free one", async ({ page }) => {
    // Free and Enterprise are both unpurchasable; pricing Enterprise at zero
    // would be the single worst thing this page could say.
    const card = planCard(page, "Enterprise");
    await expect(card).toBeVisible();
    await expect(card.locator(".price-now")).toHaveText("Custom");
  });

  test("does not render unlimited context as zero", async ({ page }) => {
    const card = planCard(page, "Enterprise");
    const context = card.locator("li").filter({ has: page.getByText("context", { exact: true }) });
    await expect(context.locator("span").last()).toHaveText("Unlimited");
  });

  test("offers a conversation instead of a checkout", async ({ page }) => {
    const card = planCard(page, "Enterprise");
    await expect(card.getByRole("button", { name: "Contact sales" })).toBeVisible();
    await expect(card.getByRole("button", { name: /Upgrade to/ })).toHaveCount(0);
  });

  test("the free plan still reads as free", async ({ page }) => {
    const card = planCard(page, "Free");
    await expect(card.locator(".price-now")).toHaveText("Free");
  });
});

test.describe("contacting sales", () => {
  test("an anonymous visitor can submit without signing up", async ({ page }) => {
    // The whole reason the endpoint is public: this person has no account yet.
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
    await page.goto("/");
    await page.getByRole("button", { name: "Contact sales" }).click();
    await expect(page.getByRole("button", { name: "Send request" })).toBeDisabled();
  });

  test("a signed-in subscriber sends their token so the lead is attributed", async ({ page, api }) => {
    // A Pro subscriber asking about Enterprise is a different conversation.
    await signIn(page);
    Object.assign(api.state, PRO_SUBSCRIBER);

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
    await page.goto("/");
    await page.getByRole("button", { name: "Contact sales" }).click();
    await expect(page.getByLabel("Work email")).toBeVisible();

    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();
    expect(results.violations.map((v) => v.id)).toEqual([]);
  });
});

test.describe("the contact form is a dialog", () => {
  test("it opens over the plans instead of pushing them down", async ({ page }) => {
    await page.goto("/");

    const plans = page.locator(".card.plan");
    // Position in the *document*, not the viewport: opening a modal moves focus
    // into it and the browser may scroll, which changes the viewport position
    // of everything without moving anything on the page.
    const topInDocument = () =>
      plans.first().evaluate((el) => Math.round(el.getBoundingClientRect().top + window.scrollY));
    const before = await topInDocument();

    await page.getByRole("button", { name: "Contact sales" }).click();

    await expect(page.getByRole("dialog")).toBeVisible();
    await expect(page.getByLabel("Work email")).toBeVisible();
    expect(await topInDocument()).toBe(before);
  });

  test("Escape closes it", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Contact sales" }).click();
    await expect(page.getByRole("dialog")).toBeVisible();

    await page.keyboard.press("Escape");

    await expect(page.getByRole("dialog")).toBeHidden();
    // The browser closes a native dialog on Escape by itself. Only reopening it
    // shows that the page's own open state was reset too.
    await page.getByRole("button", { name: "Contact sales" }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
  });

  test("so does the close button", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Contact sales" }).click();

    await page.getByRole("button", { name: "Close" }).click();

    await expect(page.getByRole("dialog")).toBeHidden();
  });
});
