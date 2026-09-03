"use client";

import { useCallback, useEffect, useState } from "react";

import { ApiError, getEntitlements, getPlans, openPortal, startCheckout } from "@/lib/api";
import type { Entitlements, Interval, Plan } from "@/lib/types";
import { TIER_RANK, formatLimit, formatMoney } from "@/lib/types";
import { useUser } from "@/lib/user";

export default function PricingPage() {
  const { userId, ready } = useUser();
  const [plans, setPlans] = useState<Plan[]>([]);
  const [ents, setEnts] = useState<Entitlements | null>(null);
  const [interval, setInterval] = useState<Interval>("monthly");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!ready) return;
    Promise.all([getPlans(userId), getEntitlements(userId)])
      .then(([p, e]) => {
        setPlans(p);
        setEnts(e);
      })
      .catch((err: Error) => setError(err.message));
  }, [userId, ready]);

  const upgrade = useCallback(
    async (tier: string) => {
      if (!userId) return;
      setBusy(tier);
      setError(null);
      try {
        const { checkout_url } = await startCheckout(userId, tier, interval);
        // Stripe's hosted page. Nothing is granted here -- the webhook does that.
        window.location.href = checkout_url;
      } catch (err) {
        setError(err instanceof ApiError ? err.message : String(err));
        setBusy(null);
      }
    },
    [userId, interval],
  );

  const manage = useCallback(async () => {
    if (!ready) return;
    setBusy("portal");
    setError(null);
    try {
      const { portal_url } = await openPortal(userId);
      window.location.href = portal_url;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setBusy(null);
    }
  }, [userId, ready]);

  if (!ready) return <p className="muted">Loading…</p>;

  const currentTier = ents?.tier ?? "free";
  const subscribed = ents?.source === "subscription";

  return (
    <>
      <h1>Plans</h1>
      <p className="lede">
        Everything on this page comes from the API — limits from the service&apos;s plan
        config, amounts from Stripe. Nothing here is hard-coded, so changing a tier never
        means shipping the frontend again.
      </p>

      {error && (
        <div className="banner error">
          <strong>Could not continue.</strong> {error}
        </div>
      )}

      <div className="inline" style={{ marginBottom: 20 }}>
        <span className="muted">Billing period</span>
        <button
          className={interval === "monthly" ? "primary" : undefined}
          onClick={() => setInterval("monthly")}
        >
          Monthly
        </button>
        <button
          className={interval === "annual" ? "primary" : undefined}
          onClick={() => setInterval("annual")}
        >
          Annual
        </button>
      </div>

      <div className="grid-3">
        {plans.map((plan) => {
          const isCurrent = plan.tier === currentTier;
          const price = plan.prices[interval];
          const isUpgrade = TIER_RANK[plan.tier] > TIER_RANK[currentTier];

          return (
            <div key={plan.tier} className={`card plan${isCurrent ? " current" : ""}`}>
              <div className="tag">{isCurrent ? "Current plan" : ""}</div>
              <h3>{plan.display_name}</h3>
              <div className="mono" style={{ color: "var(--muted)", marginTop: 2 }}>
                {plan.purchasable ? formatMoney(price, interval) : "Free"}
              </div>

              <ul>
                {plan.quotas.map((quota) => (
                  <li key={quota.key}>
                    <span>{quota.key.replace(/_/g, " ")}</span>
                    <span>{formatLimit(quota.limit)}</span>
                  </li>
                ))}
                <li>
                  <span>models</span>
                  <span>{(plan.features.models ?? []).join(", ")}</span>
                </li>
                <li>
                  <span>context</span>
                  <span>{(plan.features.context_tokens ?? 0).toLocaleString()}</span>
                </li>
                <li>
                  <span>api access</span>
                  <span>{plan.features.api_access ? "yes" : "—"}</span>
                </li>
                <li>
                  <span>support</span>
                  <span>{String(plan.features.support_sla ?? "—")}</span>
                </li>
              </ul>

              <div className="actions">
                {isCurrent ? (
                  <button disabled>Current plan</button>
                ) : subscribed ? (
                  // An existing subscriber changes plan in Stripe's portal, where
                  // proration is handled. A second checkout would create a second
                  // subscription, and the API refuses it with a 409.
                  <button onClick={manage} disabled={busy !== null}>
                    {busy === "portal" ? "Opening…" : "Change in portal"}
                  </button>
                ) : plan.purchasable ? (
                  <button
                    className={isUpgrade ? "primary" : undefined}
                    onClick={() => upgrade(plan.tier)}
                    disabled={busy !== null || !price}
                  >
                    {busy === plan.tier ? "Redirecting…" : `Upgrade to ${plan.display_name}`}
                  </button>
                ) : (
                  <button disabled>Included</button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}
