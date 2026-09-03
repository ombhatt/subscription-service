import { test as base, type Page } from "@playwright/test";

import type { Entitlements, Plan, Tier } from "../lib/types";

/**
 * A stand-in for the API, installed as route handlers.
 *
 * The backend has its own suite for its own logic; what these tests need is the
 * ability to put the *frontend* in states that are slow or impossible to
 * produce for real -- a subscriber inside a dunning grace window, a webhook
 * that never arrives, a quota already at its ceiling.
 *
 * Shapes here must match app/schemas.py. When they drift, the contract tests in
 * `contract.spec.ts` are what should fail.
 */

const FEATURES: Record<Tier, Entitlements["features"]> = {
  free: {
    models: ["small"],
    context_tokens: 32_000,
    history_retention_days: 30,
    api_access: false,
    support_sla: "community",
  },
  plus: {
    models: ["small", "large"],
    context_tokens: 200_000,
    history_retention_days: 365,
    api_access: false,
    support_sla: "48h",
  },
  pro: {
    models: ["small", "large", "reasoning"],
    context_tokens: 1_000_000,
    history_retention_days: null,
    api_access: true,
    support_sla: "8h",
  },
};

const MESSAGE_LIMITS: Record<Tier, number | null> = { free: 20, plus: 300, pro: 1500 };
const UPLOAD_LIMITS: Record<Tier, number | null> = { free: 3, plus: 50, pro: null };
const DISPLAY: Record<Tier, string> = { free: "Free", plus: "Plus", pro: "Pro" };

export interface FakeState {
  tier: Tier;
  status: string;
  source: Entitlements["source"];
  messagesUsed: number;
  currentPeriodEnd: string | null;
  cancelAtPeriodEnd: boolean;
  graceEndsAt: string | null;
  /** Bumps the tier to this after N entitlement reads, to imitate a webhook landing. */
  grantAfterReads?: { reads: number; tier: Tier };
}

export class FakeApi {
  state: FakeState;
  entitlementReads = 0;
  checkoutCalls = 0;
  portalCalls = 0;

  constructor(overrides: Partial<FakeState> = {}) {
    this.state = {
      tier: "free",
      status: "free",
      source: "default",
      messagesUsed: 0,
      currentPeriodEnd: null,
      cancelAtPeriodEnd: false,
      graceEndsAt: null,
      ...overrides,
    };
  }

  entitlements(userId: string): Entitlements {
    this.entitlementReads += 1;

    const pending = this.state.grantAfterReads;
    if (pending && this.entitlementReads >= pending.reads) {
      this.state.tier = pending.tier;
      this.state.status = "active";
      this.state.source = "subscription";
      this.state.currentPeriodEnd = "2026-10-03T03:43:47Z";
    }

    const { tier } = this.state;
    const messageLimit = MESSAGE_LIMITS[tier];
    const uploadLimit = UPLOAD_LIMITS[tier];

    return {
      user_id: userId,
      tier,
      display_name: DISPLAY[tier],
      status: this.state.status,
      source: this.state.source,
      features: FEATURES[tier],
      quotas: [
        {
          key: "messages_per_day",
          limit: messageLimit,
          used: this.state.messagesUsed,
          remaining:
            messageLimit === null ? null : Math.max(0, messageLimit - this.state.messagesUsed),
          reset_at: "2026-09-04T00:00:00Z",
        },
        {
          key: "file_uploads_per_day",
          limit: uploadLimit,
          used: 0,
          remaining: uploadLimit,
          reset_at: "2026-09-04T00:00:00Z",
        },
      ],
      current_period_end: this.state.currentPeriodEnd,
      cancel_at_period_end: this.state.cancelAtPeriodEnd,
      grace_ends_at: this.state.graceEndsAt,
    };
  }

  plans(): Plan[] {
    const tiers: Tier[] = ["free", "plus", "pro"];
    return tiers.map((tier) => ({
      tier,
      display_name: DISPLAY[tier],
      purchasable: tier !== "free",
      features: FEATURES[tier],
      quotas: [
        { key: "messages_per_day", limit: MESSAGE_LIMITS[tier], window: "daily" },
        { key: "file_uploads_per_day", limit: UPLOAD_LIMITS[tier], window: "daily" },
      ],
      prices:
        tier === "free"
          ? {}
          : {
              monthly: {
                price_id: `price_${tier}_m`,
                unit_amount: tier === "plus" ? 2000 : 10000,
                currency: "usd",
              },
              annual: {
                price_id: `price_${tier}_y`,
                unit_amount: tier === "plus" ? 20000 : 100000,
                currency: "usd",
              },
            },
    }));
  }
}

/** Installs the fake on a page. Call before navigating. */
export async function mockApi(page: Page, api: FakeApi) {
  // Registered first, so every specific handler below outranks it. Anything
  // that reaches here is a call nobody mocked -- which would otherwise be
  // proxied to the real service by the Next rewrite. Fail loudly instead.
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    await route.fulfill({
      status: 599,
      json: {
        error: "unmocked_api_call",
        message: `No fake installed for ${request.method()} ${new URL(request.url()).pathname}`,
      },
    });
  });

  await page.route("**/api/v1/billing/plans", async (route) => {
    await route.fulfill({ json: api.plans() });
  });

  await page.route("**/api/v1/entitlements", async (route) => {
    const userId = route.request().headers()["x-user-id"] ?? "unknown";
    await route.fulfill({ json: api.entitlements(userId) });
  });

  await page.route("**/api/v1/billing/checkout", async (route) => {
    api.checkoutCalls += 1;
    // A real redirect would leave the app; the test only needs to know the
    // call was made with the right body.
    await route.fulfill({
      json: { checkout_url: "http://localhost:3000/billing/success", session_id: "cs_test_fake" },
    });
  });

  await page.route("**/api/v1/billing/portal", async (route) => {
    api.portalCalls += 1;
    await route.fulfill({ json: { portal_url: "http://localhost:3000/billing?portal=1" } });
  });

  await page.route("**/api/v1/chat", async (route) => {
    const body = route.request().postDataJSON() as { model: string; message: string };
    const { tier } = api.state;
    const allowed = FEATURES[tier].models ?? [];

    // Feature check first, before any quota is consumed -- same order as the API.
    if (!allowed.includes(body.model)) {
      const required = (["free", "plus", "pro"] as Tier[]).find((t) =>
        (FEATURES[t].models ?? []).includes(body.model),
      );
      await route.fulfill({
        status: 403,
        json: {
          error: "feature_not_entitled",
          feature: `model:${body.model}`,
          current_tier: tier,
          required_tier: required ?? null,
        },
      });
      return;
    }

    const limit = MESSAGE_LIMITS[tier];
    if (limit !== null && api.state.messagesUsed >= limit) {
      await route.fulfill({
        status: 429,
        json: {
          error: "quota_exceeded",
          quota: "messages_per_day",
          limit,
          used: limit + 1,
          remaining: 0,
          reset_at: "2026-09-04T00:00:00Z",
          current_tier: tier,
          upgrade_tier: tier === "free" ? "plus" : tier === "plus" ? "pro" : null,
        },
      });
      return;
    }

    api.state.messagesUsed += 1;
    await route.fulfill({
      json: {
        model: body.model,
        reply: `[${tier}] echo: ${body.message}`,
        quota: {
          key: "messages_per_day",
          limit,
          used: api.state.messagesUsed,
          remaining: limit === null ? null : limit - api.state.messagesUsed,
        },
      },
    });
  });
}

/** Sets the demo user before any script runs, so the first render already has it. */
export async function signInAs(page: Page, userId: string) {
  await page.addInitScript((id) => {
    window.localStorage.setItem("demo-user-id", id);
  }, userId);
}

export const test = base.extend<{ api: FakeApi }>({
  // `auto` matters: as a lazy fixture, any test that did not destructure `api`
  // installed no routes at all and its requests went through the Next rewrite
  // to the real service -- mutating real quota counters and making results
  // depend on whatever state that service happened to be in. Automatic setup
  // means no test can reach the live backend by omission.
  api: [
    async ({ page }, use) => {
      const api = new FakeApi();
      await mockApi(page, api);
      await use(api);
    },
    { auto: true },
  ],
});

export { expect } from "@playwright/test";
