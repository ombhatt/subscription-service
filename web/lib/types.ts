/**
 * The API's shapes, generated from its OpenAPI schema into openapi.gen.ts by
 * `make api-types` and checked in CI -- not copied by hand from app/schemas.py,
 * which is how these used to drift. Change the pydantic model, regenerate, and
 * the compiler finds every use that no longer fits.
 */
import type { components } from "./openapi.gen";

type Schemas = components["schemas"];

export type Tier = Schemas["Tier"];
export type Interval = Schemas["BillingInterval"];
export type QuotaState = Schemas["QuotaState"];
export type Entitlements = Schemas["EntitlementResponse"];
export type Price = Schemas["PlanPrice"];
export type Discount = Schemas["Discount"];
export type SubscriptionSummary = Schemas["SubscriptionSummary"];
export type ChatReply = Schemas["ChatReply"];
export type ContactSalesPayload = Schemas["ContactSalesRequest"];
export type CheckoutPayload = Schemas["CheckoutRequest"];
/**
 * The generator marks `interval` required because the server defaults it, but
 * a request may omit it. "Manage billing" sends neither field.
 */
export type PortalPayload = Partial<Schemas["PortalRequest"]>;

/**
 * The generator reads `dict[BillingInterval, PlanPrice]` as any string key. The
 * keys are intervals, and a tier with no price for one simply lacks it.
 */
export type Plan = Omit<Schemas["PlanResponse"], "prices"> & {
  prices: Partial<Record<Interval, Price>>;
};

/**
 * What to show for a plan: an amount, optionally struck through against a list
 * price, optionally labelled.
 *
 * Deliberately an object rather than a number. Today every offer is just the
 * list price, but a promo code — and later anything that decides a price per
 * user — changes only what this function returns, never the component that
 * renders it. That is the whole point of the indirection.
 */
export interface Offer {
  amount: number | null;
  compareAt: number | null;
  currency: string | null;
  label: string | null;
  interval: Interval;
}

export function offerFor(
  price: Price | undefined,
  interval: Interval,
  discount?: Discount | null,
): Offer {
  const base: Offer = {
    amount: price?.unit_amount ?? null,
    compareAt: null,
    currency: price?.currency ?? null,
    label: null,
    interval,
  };
  if (!discount || base.amount === null) return base;

  if (discount.percent_off) {
    return {
      ...base,
      amount: Math.round(base.amount * (1 - discount.percent_off / 100)),
      compareAt: base.amount,
      label: describeDiscount(discount),
    };
  }
  if (discount.amount_off) {
    return {
      ...base,
      amount: Math.max(0, base.amount - discount.amount_off),
      compareAt: base.amount,
      label: describeDiscount(discount),
    };
  }
  return base;
}

export function describeDiscount(discount: Discount): string {
  const size = discount.percent_off
    ? `${discount.percent_off}% off`
    : discount.amount_off
      ? `${formatAmount(discount.amount_off, discount.currency)} off`
      : "Discount";

  if (discount.duration === "forever") return `${size}, for as long as you subscribe`;
  if (discount.duration === "once") return `${size} on your first invoice`;
  if (discount.duration === "repeating" && discount.duration_in_months) {
    return `${size} for ${discount.duration_in_months} months`;
  }
  return size;
}

export function formatAmount(minorUnits: number, currency: string | null): string {
  const amount = minorUnits / 100;
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: (currency ?? "usd").toUpperCase(),
    minimumFractionDigits: amount % 1 === 0 ? 0 : 2,
  }).format(amount);
}

// Error bodies are built by app/errors.py's handlers rather than declared as
// response models, so they are not in the schema and stay written out here.

/** 429 body from the quota middleware. */
export interface QuotaExceeded {
  error: "quota_exceeded";
  quota: string;
  limit: number;
  used: number;
  remaining: number;
  reset_at: string;
  current_tier: Tier;
  upgrade_tier: Tier | null;
}

/** 403 body when a feature is not on this tier. */
export interface FeatureNotEntitled {
  error: "feature_not_entitled";
  feature: string;
  current_tier: Tier;
  required_tier: Tier | null;
}

export const TIER_RANK: Record<Tier, number> = { free: 0, plus: 1, pro: 2, enterprise: 3 };

/**
 * `messages_per_day` -> `Messages per day`.
 *
 * Derived rather than looked up, so a quota or feature added to plans.py gets a
 * readable name without a matching edit here -- the same reason nothing else in
 * this app keeps its own copy of what a tier contains. Raw keys were being
 * rendered straight to screen, which reads badly and is worse aloud: some
 * screen readers announce the underscores.
 */
export function humanizeKey(key: string): string {
  const words = key.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function formatLimit(limit: number | null): string {
  return limit === null ? "Unlimited" : limit.toLocaleString();
}

export function formatDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}
