/**
 * Accessibility.
 *
 * Two halves, because they catch different things.
 *
 * `axe` catches the mechanical faults -- a control with no name, contrast below
 * threshold, a broken heading order. It is fast, it covers every page, and it
 * will notice a regression nobody thought to write a test for.
 *
 * What axe cannot tell you is whether the page is *usable*: it has no opinion
 * on whether a paywall that appears after a failed request is ever announced,
 * or whether "nearly out of messages" survives being read aloud rather than
 * looked at. Those are the second half, asserted by hand.
 */

import AxeBuilder from "@axe-core/playwright";

import { FakeApi, expect, mockApi, signIn, test } from "./fixtures";

async function violations(page: import("@playwright/test").Page) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  return results.violations.map((v) => ({
    id: v.id,
    impact: v.impact,
    nodes: v.nodes.map((n) => n.target.join(" ")),
  }));
}

// --------------------------------------------------------------------------
// the mechanical half
// --------------------------------------------------------------------------

test.describe("no WCAG A/AA violations", () => {
  test("the pricing page, signed out", async ({ page }) => {
    await mockApi(page, new FakeApi());
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });

  test("the sign-in page", async ({ page }) => {
    await page.goto("/login");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });

  test("billing, with every banner showing at once", async ({ page }) => {
    // Deliberately the worst case: dunning, a pending cancellation and a
    // discount all on screen, which is the state most likely to break.
    await signIn(page);
    await mockApi(
      page,
      new FakeApi({
        tier: "plus",
        status: "past_due",
        source: "subscription",
        messagesUsed: 290,
        currentPeriodEnd: "2026-10-03T03:43:47Z",
        cancelAtPeriodEnd: true,
        graceEndsAt: "2026-09-11T00:00:00Z",
        discount: {
          coupon_id: "co_1",
          name: "Launch",
          percent_off: 25,
          amount_off: null,
          currency: "usd",
          duration: "repeating",
          duration_in_months: 3,
          promotion_code: "LAUNCH25",
          ends_at: null,
        },
      }),
    );
    await page.goto("/billing");
    await expect(page.getByRole("heading", { name: /Plus/ })).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });

  test("chat, after hitting the paywall", async ({ page }) => {
    await signIn(page);
    await mockApi(page, new FakeApi({ tier: "free", messagesUsed: 20 }));
    await page.goto("/chat");
    await page.getByLabel("Message").fill("hello");
    await page.getByRole("button", { name: "Send" }).click();
    await expect(page.getByText(/Daily limit reached/)).toBeVisible();
    expect(await violations(page)).toEqual([]);
  });
});

// --------------------------------------------------------------------------
// the half axe cannot check
// --------------------------------------------------------------------------

test.describe("things that must be announced, not just drawn", () => {
  test("a failed request is announced assertively", async ({ page }) => {
    await signIn(page);
    const api = new FakeApi({ tier: "free" });
    await mockApi(page, api);
    await page.route("**/api/v1/billing/portal", (route) =>
      route.fulfill({ status: 404, json: { detail: "no billing account yet" } }),
    );
    await page.goto("/billing");
    await page.getByRole("button", { name: "Manage billing" }).click();

    // role="alert" interrupts. An error the user caused by acting is the one
    // case where interrupting is right.
    //
    // Filtered by text because Next ships its own always-present
    // #__next-route-announcer__ with role="alert"; an unfiltered getByRole
    // matches two elements and fails on strict mode rather than on substance.
    const alert = page.getByRole("alert").filter({ hasText: /went wrong/i });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText(/went wrong/i);
  });

  test("the paywall is announced politely, not silently", async ({ page }) => {
    await signIn(page);
    await mockApi(page, new FakeApi({ tier: "free", messagesUsed: 20 }));
    await page.goto("/chat");
    await page.getByLabel("Message").fill("hello");
    await page.getByRole("button", { name: "Send" }).click();

    const status = page.getByRole("status").filter({ hasText: /Daily limit reached/ });
    await expect(status).toBeVisible();
  });

  test("dunning and cancellation notices are in live regions", async ({ page }) => {
    await signIn(page);
    await mockApi(
      page,
      new FakeApi({
        tier: "plus",
        status: "past_due",
        source: "subscription",
        graceEndsAt: "2026-09-11T00:00:00Z",
        cancelAtPeriodEnd: true,
        currentPeriodEnd: "2026-10-03T03:43:47Z",
      }),
    );
    await page.goto("/billing");

    await expect(
      page.getByRole("status").filter({ hasText: /last payment failed/i }),
    ).toBeVisible();
    await expect(
      page.getByRole("status").filter({ hasText: /Subscription ending/i }),
    ).toBeVisible();
  });
});

test.describe("the quota meter conveys its value without being seen", () => {
  test("it exposes the count, the cap and a readable name", async ({ page }) => {
    await signIn(page);
    await mockApi(
      page,
      new FakeApi({ tier: "plus", status: "active", source: "subscription", messagesUsed: 12 }),
    );
    await page.goto("/billing");

    const meter = page.getByRole("progressbar", { name: "Messages per day" });
    await expect(meter).toBeVisible();
    await expect(meter).toHaveAttribute("aria-valuenow", "12");
    await expect(meter).toHaveAttribute("aria-valuemax", "300");
    // "4 percent" is true and useless; the numbers are what matter.
    await expect(meter).toHaveAttribute("aria-valuetext", "12 of 300 used");
  });

  test("the raw quota key is never what the user reads", async ({ page }) => {
    await signIn(page);
    await mockApi(page, new FakeApi({ tier: "plus", status: "active", source: "subscription" }));
    await page.goto("/billing");

    await expect(page.getByText("messages_per_day")).toHaveCount(0);
    await expect(page.getByText("Messages per day")).toBeVisible();
  });

  test("being nearly out is said in words, not only in colour", async ({ page }) => {
    // Red on a bar is invisible to a screen reader and ambiguous to roughly one
    // man in twelve looking straight at it.
    await signIn(page);
    await mockApi(
      page,
      new FakeApi({
        tier: "plus",
        status: "active",
        source: "subscription",
        messagesUsed: 295, // 98% of 300
      }),
    );
    await page.goto("/billing");
    await expect(page.getByText(/Nearly used up/i)).toBeVisible();
  });

  test("an unlimited quota exposes no meter to misread", async ({ page }) => {
    await signIn(page);
    await mockApi(page, new FakeApi({ tier: "pro", status: "active", source: "subscription" }));
    await page.goto("/billing");

    await expect(
      page.getByRole("progressbar", { name: "File uploads per day" }),
    ).toHaveCount(0);
  });
});

test.describe("keyboard and structure", () => {
  test("the first tab stop skips the navigation", async ({ page }) => {
    await mockApi(page, new FakeApi());
    await page.goto("/");
    await page.keyboard.press("Tab");

    const focused = page.locator(":focus");
    await expect(focused).toHaveText(/skip to main content/i);
    await focused.press("Enter");
    await expect(page.locator("main")).toBeFocused();
  });

  test("the current page is marked, not merely underlined", async ({ page }) => {
    await signIn(page);
    await mockApi(page, new FakeApi());
    await page.goto("/billing");
    await expect(page.getByRole("link", { name: "Billing" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  test("every chat control has a name", async ({ page }) => {
    await signIn(page);
    await mockApi(page, new FakeApi());
    await page.goto("/chat");

    // A placeholder is not a label: it vanishes on focus.
    await expect(page.getByLabel("Message")).toBeVisible();
    await expect(page.getByLabel("Model")).toBeVisible();
  });

  test("replies land in a live region", async ({ page }) => {
    await signIn(page);
    await mockApi(page, new FakeApi({ tier: "plus" }));
    await page.goto("/chat");
    await page.getByLabel("Message").fill("hello");
    await page.getByRole("button", { name: "Send" }).click();

    const log = page.getByRole("log", { name: "Conversation" });
    await expect(log).toContainText("echo: hello");
  });
});
