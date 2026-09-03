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
