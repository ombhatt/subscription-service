/** Mirrors the API's response shapes. Keep in step with app/schemas.py. */

export type Tier = "free" | "plus" | "pro";

export interface QuotaState {
  key: string;
  limit: number | null; // null means unlimited
  used: number;
  remaining: number | null;
  reset_at: string;
}

export interface Entitlements {
  user_id: string;
  tier: Tier;
  display_name: string;
  status: string;
  source: "subscription" | "grant" | "default";
  features: {
    models?: string[];
    context_tokens?: number;
    history_retention_days?: number | null;
    api_access?: boolean;
    support_sla?: string;
    [key: string]: unknown;
  };
  quotas: QuotaState[];
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  grace_ends_at: string | null;
}

export type Interval = "monthly" | "annual";

export interface Price {
  price_id: string;
  /** In the currency's smallest unit, as Stripe stores it. Null if unreadable. */
  unit_amount: number | null;
  currency: string | null;
}

export interface Plan {
  tier: Tier;
  display_name: string;
  purchasable: boolean;
  features: Entitlements["features"];
  quotas: { key: string; limit: number | null; window: string }[];
  prices: Partial<Record<Interval, Price>>;
}

/** A discount mirrored from Stripe onto the subscription. */
export interface Discount {
  coupon_id: string | null;
  name: string | null;
  percent_off: number | null;
  amount_off: number | null;
  currency: string | null;
  duration: string | null;
  duration_in_months: number | null;
  promotion_code: string | null;
  /** Unix seconds, or null for a discount that never ends. */
  ends_at: number | null;
}

export interface SubscriptionSummary {
  tier: Tier;
  status: string;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  discount: Discount | null;
}

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

export function formatMoney(price: Price | undefined, interval: Interval): string {
  if (!price || price.unit_amount === null || !price.currency) return "—";
  const amount = price.unit_amount / 100;
  const formatted = new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: price.currency.toUpperCase(),
    // Whole-dollar prices read better without the trailing .00
    minimumFractionDigits: amount % 1 === 0 ? 0 : 2,
  }).format(amount);
  return `${formatted}/${interval === "annual" ? "yr" : "mo"}`;
}

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

export interface ChatReply {
  model: string;
  reply: string;
  quota: { key: string; limit: number | null; used: number; remaining: number | null };
}

export const TIER_RANK: Record<Tier, number> = { free: 0, plus: 1, pro: 2 };

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
