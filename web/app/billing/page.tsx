"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import QuotaMeter from "@/components/QuotaMeter";
import { ApiError, getEntitlements, openPortal } from "@/lib/api";
import type { Entitlements } from "@/lib/types";
import { formatDate } from "@/lib/types";
import { useUser } from "@/lib/user";

export default function BillingPage() {
  const { userId, ready } = useUser();
  const [ents, setEnts] = useState<Entitlements | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!ready) return;
    getEntitlements(userId)
      .then(setEnts)
      .catch((err: Error) => setError(err.message));
  }, [userId, ready]);

  const manage = useCallback(async () => {
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      const { portal_url } = await openPortal(userId);
      window.location.href = portal_url;
    } catch (err) {
      // 404 here means "no billing account yet" -- a free user who has never
      // checked out has nothing for the portal to show.
      setError(err instanceof ApiError ? err.message : String(err));
      setBusy(false);
    }
  }, [userId, ready]);

  if (!ready || (!ents && !error)) return <p className="muted">Loading…</p>;

  return (
    <>
      <h1>Billing</h1>
      <p className="lede">
        What <code>{userId}</code> is entitled to right now, straight from{" "}
        <code>GET /v1/entitlements</code> — the one call the product makes.
      </p>

      {error && (
        <div className="banner error">
          <strong>Something went wrong.</strong> {error}
        </div>
      )}

      {ents && (
        <div className="stack">
          {ents.grace_ends_at && (
            <div className="banner warn">
              <strong>Your last payment failed.</strong> You keep {ents.display_name} access
              until {formatDate(ents.grace_ends_at)} while we retry. Update your card in the
              billing portal to avoid dropping to Free.
            </div>
          )}

          {ents.cancel_at_period_end && (
            <div className="banner warn">
              <strong>Subscription ending.</strong> You keep {ents.display_name} until{" "}
              {formatDate(ents.current_period_end)}, then move to Free. You can reactivate in
              the portal any time before then.
            </div>
          )}

          {ents.source === "grant" && (
            <div className="banner">
              <strong>Complimentary access.</strong> Your {ents.display_name} plan was granted
              directly rather than purchased.
            </div>
          )}

          <div className="card">
            <div className="inline" style={{ justifyContent: "space-between" }}>
              <div>
                <h2 style={{ marginBottom: 4 }}>
                  {ents.display_name}{" "}
                  <span className={`pill${ents.status === "active" ? " live" : ""}`}>
                    {ents.status}
                  </span>
                </h2>
                <p className="muted" style={{ margin: 0 }}>
                  {ents.source === "subscription"
                    ? `Renews ${formatDate(ents.current_period_end)}`
                    : ents.source === "grant"
                      ? "Granted plan"
                      : "No paid subscription"}
                </p>
              </div>
              <div className="inline">
                <Link className="btn" href="/">
                  {ents.tier === "free" ? "See plans" : "Change plan"}
                </Link>
                <button className="primary" onClick={manage} disabled={busy}>
                  {busy ? "Opening…" : "Manage billing"}
                </button>
              </div>
            </div>
          </div>

          <div className="card">
            <h2>Usage</h2>
            {ents.quotas.map((quota) => (
              <QuotaMeter key={quota.key} quota={quota} />
            ))}
          </div>

          <div className="card">
            <h2>What this plan includes</h2>
            <div className="rows">
              <div className="row-item">
                <span className="k">models</span>
                <span className="v">{(ents.features.models ?? []).join(", ")}</span>
              </div>
              <div className="row-item">
                <span className="k">context_tokens</span>
                <span className="v">
                  {(ents.features.context_tokens ?? 0).toLocaleString()}
                </span>
              </div>
              <div className="row-item">
                <span className="k">history_retention_days</span>
                <span className="v">
                  {ents.features.history_retention_days === null
                    ? "Unlimited"
                    : String(ents.features.history_retention_days)}
                </span>
              </div>
              <div className="row-item">
                <span className="k">api_access</span>
                <span className="v">{ents.features.api_access ? "Yes" : "No"}</span>
              </div>
              <div className="row-item">
                <span className="k">support_sla</span>
                <span className="v">{String(ents.features.support_sla ?? "—")}</span>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
