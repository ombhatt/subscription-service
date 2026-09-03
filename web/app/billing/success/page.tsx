"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { getEntitlements } from "@/lib/api";
import type { Entitlements } from "@/lib/types";
import { useUser } from "@/lib/user";

/**
 * Where Stripe sends the customer back to.
 *
 * This page grants nothing. It cannot: the payment is confirmed to Stripe, and
 * the webhook is what tells our service about it. So the honest thing for a
 * success page to do is *poll* until the entitlement actually changes, and say
 * plainly what is happening in the meantime.
 *
 * That is also why closing this tab is harmless -- the grant does not depend on
 * anyone seeing this page.
 */

const POLL_INTERVAL_MS = 1000;
const MAX_ATTEMPTS = 15;

export default function SuccessPage() {
  const { userId, ready } = useUser();
  const [ents, setEnts] = useState<Entitlements | null>(null);
  const [attempts, setAttempts] = useState(0);
  const [gaveUp, setGaveUp] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!ready) return;
    let cancelled = false;
    let tries = 0;

    async function poll() {
      if (cancelled) return;
      tries += 1;
      setAttempts(tries);
      try {
        const current = await getEntitlements(userId);
        if (cancelled) return;
        setEnts(current);
        if (current.tier !== "free") return; // granted; stop polling
      } catch {
        // Keep trying; a transient failure here is not worth showing.
      }
      if (tries >= MAX_ATTEMPTS) {
        setGaveUp(true);
        return;
      }
      timer.current = setTimeout(poll, POLL_INTERVAL_MS);
    }

    poll();
    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [userId, ready]);

  if (!ready) return <p className="muted">Loading…</p>;

  const granted = ents !== null && ents.tier !== "free";

  return (
    <>
      <h1>{granted ? `You're on ${ents!.display_name}` : "Confirming your payment"}</h1>

      {granted ? (
        <>
          <p className="lede">
            Your payment went through and the subscription is active. Access was granted by
            Stripe&apos;s webhook, not by this page — which is why it would have worked even if
            you had closed the tab.
          </p>
          <div className="inline">
            <Link className="btn" href="/billing">
              View billing
            </Link>
            <Link className="btn" href="/chat">
              Try it out
            </Link>
          </div>
        </>
      ) : gaveUp ? (
        <>
          <div className="banner warn">
            <strong>Still waiting on the webhook.</strong> Stripe has your payment, but this
            service has not been told about it yet, so you are still on Free.
          </div>
          <p className="lede">
            In development this almost always means <code>stripe listen</code> is not running,
            or its <code>whsec_…</code> is not in the API&apos;s <code>.env</code>. The payment
            is not lost — the subscription will appear as soon as the event is delivered, or on
            the next reconciliation run.
          </p>
          <Link className="btn" href="/billing">
            Check billing
          </Link>
        </>
      ) : (
        <>
          <p className="lede">
            <span className="spin" /> Waiting for Stripe to confirm the subscription. This
            usually takes a second or two.
          </p>
          <p className="muted">
            Attempt {attempts} of {MAX_ATTEMPTS}. You can safely leave this page.
          </p>
        </>
      )}
    </>
  );
}
