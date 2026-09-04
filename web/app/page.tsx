"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ApiError, getEntitlements, getPlans, openPortal, startCheckout } from "@/lib/api";
import type { Entitlements, Interval, Plan } from "@/lib/types";
import { TIER_RANK, formatLimit, formatMoney } from "@/lib/types";
import { useUser } from "@/lib/user";

export default function PricingPage() {
  const { userId, ready } = useUser();
  const router = useRouter();
  const [plans, setPlans] = useState<Plan[]>([]);
  const [ents, setEnts] = useState<Entitlements | null>(null);
  const [interval, setInterval] = useState<Interval>("monthly");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!ready) return;
    // Plans are public: this is the pricing page, and has to render for someone
    // who has never signed up.
    getPlans()
      .then(setPlans)
      .catch((err: Error) => setError(err.message));

    if (!userId) {
      setEnts(null);
      return;
    }
    getEntitlements()
      .then(setEnts)
      .catch(() => setEnts(null));
  }, [userId, ready]);

  const upgrade = useCallback(
    async (tier: string) => {
      if (!userId) {
        // Signed out: they can read the prices, they just cannot buy yet.
        router.push("/login?next=/");
        return;
      }
      setBusy(tier);
      setError(null);
      try {
        const { checkout_url } = await startCheckout(tier, interval);
        // Stripe's hosted page. Nothing is granted here -- the webhook does that.
        window.location.href = checkout_url;
      } catch (err) {
        setError(err instanceof ApiError ? err.message : String(err));
        setBusy(null);
      }
    },
    [userId, interval, router],
  );

  const manage = useCallback(async () => {
    if (!ready) return;
    setBusy("portal");
    setError(null);
    try {
      const { portal_url } = await openPortal();
      window.location.href = portal_url;
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setBusy(null);
    }
  }, [userId, ready]);

  if (!ready) return <p className="muted">Loading…</p>;

  // Null, not "free", when signed out: someone without an account has no
  // current plan, and telling them Free is "their" plan is a small lie that
  // makes the page look like it already knows them.
  const currentTier = ents?.tier ?? null;
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
          const isCurrent = currentTier !== null && plan.tier === currentTier;
          const price = plan.prices[interval];
          // With no session every paid tier is an upgrade from nothing.
          const isUpgrade = TIER_RANK[plan.tier] > (currentTier ? TIER_RANK[currentTier] : -1);

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
