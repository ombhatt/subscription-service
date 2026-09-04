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
    // The real API derives the user from the bearer token; the fake only needs
    // to notice that one was sent.
    const authorization = route.request().headers()["authorization"] ?? "";
    if (!authorization.toLowerCase().startsWith("bearer ")) {
      await route.fulfill({ status: 401, json: { detail: "missing bearer token" } });
      return;
    }
    await route.fulfill({ json: api.entitlements(TEST_USER_ID) });
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

export const TEST_USER_ID = "8f14e45f-ceea-467a-9c1e-3f2a1b6c7d80";
export const TEST_EMAIL = "alice@example.test";

function b64url(value: unknown): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

/**
 * A JWT-shaped access token. The signature is meaningless -- nothing in the
 * browser verifies it, and the API it would be sent to is mocked. The backend's
 * own suite covers real signature verification against a generated keypair.
 */
export function fakeAccessToken(userId = TEST_USER_ID, email = TEST_EMAIL): string {
  const now = Math.floor(Date.now() / 1000);
  return [
    b64url({ alg: "RS256", typ: "JWT", kid: "test" }),
    b64url({
      sub: userId,
      email,
      aud: "authenticated",
      role: "authenticated",
      iat: now,
      exp: now + 3600,
    }),
    "not-a-real-signature",
  ].join(".");
}

/**
 * Sign in by driving the actual login form, with Supabase's endpoints mocked.
 *
 * Seeding a session cookie directly would be faster but couples the tests to
 * whatever storage format @supabase/ssr uses this week. Letting the real client
 * persist its own session survives that, and exercises the login page as a
 * side effect.
 */
export async function signIn(
  page: Page,
  { userId = TEST_USER_ID, email = TEST_EMAIL } = {},
) {
  const user = {
    id: userId,
    aud: "authenticated",
    role: "authenticated",
    email,
    app_metadata: {},
    user_metadata: {},
    created_at: new Date().toISOString(),
  };
  const session = {
    access_token: fakeAccessToken(userId, email),
    token_type: "bearer",
    expires_in: 3600,
    expires_at: Math.floor(Date.now() / 1000) + 3600,
    refresh_token: "fake-refresh-token",
    user,
  };

  await page.route("**/auth/v1/token**", (route) => route.fulfill({ json: session }));
  await page.route("**/auth/v1/user**", (route) => route.fulfill({ json: user }));
  await page.route("**/auth/v1/logout**", (route) => route.fulfill({ status: 204, body: "" }));

  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("a-password-long-enough");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"));
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

      // An uncaught exception in the app otherwise surfaces as "element not
      // found" several assertions later, which is a poor way to discover that
      // a build-time environment variable was missing and every page crashed.
      const crashes: string[] = [];
      page.on("pageerror", (error) => crashes.push(error.message));

      await use(api);

      if (crashes.length > 0) {
        throw new Error(
          `The page threw an uncaught exception:\n  ${crashes.join("\n  ")}`,
        );
      }
    },
    { auto: true },
  ],
});

export { expect } from "@playwright/test";
