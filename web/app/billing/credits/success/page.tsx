"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import Banner from "@/components/Banner";
import { getCreditPurchase } from "@/lib/api";
import type { CreditPurchase } from "@/lib/types";
import { formatDate } from "@/lib/types";
import { useUser } from "@/lib/user";

/**
 * Where Stripe sends a credit pack buyer back to, with `?session_id=cs_…`.
 *
 * Like the subscription success page, this grants nothing: Stripe's webhook
 * does. So it polls for *this* purchase's grant -- not just any change in the
 * balance, which an admin grant or a spend could move -- and says plainly what
 * is happening until it lands.
 */

const POLL_INTERVAL_MS = 1000;
const MAX_ATTEMPTS = 15;

function CreditsSuccess() {
  const { userId, ready } = useUser();
  const sessionId = useSearchParams().get("session_id");
  const [purchase, setPurchase] = useState<CreditPurchase | null>(null);
  const [attempts, setAttempts] = useState(0);
  const [gaveUp, setGaveUp] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!ready || !sessionId) return;
    let cancelled = false;
    let tries = 0;

    async function poll() {
      if (cancelled) return;
      tries += 1;
      setAttempts(tries);
      try {
        const current = await getCreditPurchase(sessionId!);
        if (cancelled) return;
        setPurchase(current);
        if (current.granted) return; // landed; stop polling
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
  }, [userId, ready, sessionId]);

  if (!ready)
    return (
      <p className="muted" role="status">
        Loading…
      </p>
    );

  if (!sessionId)
    return (
      <>
        <h1>Credits</h1>
        <p className="lede">This page needs the checkout it is confirming.</p>
        <Link className="btn" href="/billing">
          Back to billing
        </Link>
      </>
    );

  const granted = purchase?.granted === true;

  return (
    <>
      <h1>{granted ? "Credits added" : "Confirming your payment"}</h1>

      {granted ? (
        <>
          <p className="lede">
            {purchase!.credits!.toLocaleString()} credits were added. Your balance is now{" "}
            <strong>{purchase!.balance.balance.toLocaleString()} credits</strong>
            {purchase!.balance.next_expiry_at
              ? `, the soonest of them expiring ${formatDate(purchase!.balance.next_expiry_at)}`
              : ""}
            . Stripe emails the receipt.
          </p>
          <div className="inline">
            <Link className="btn" href="/billing">
              View billing
            </Link>
            <Link className="btn" href="/chat">
              Keep chatting
            </Link>
          </div>
        </>
      ) : gaveUp ? (
        <>
          <Banner tone="warn">
            <strong>Still waiting on the webhook.</strong> If you paid by card, Stripe has your
            payment and this service has not been told yet. A bank payment can take a few days
            to clear, and the credits arrive when it does.
          </Banner>
          <p className="lede">
            In development this almost always means <code>stripe listen</code> is not running.
            The payment is not lost — the credits appear as soon as the event is delivered.
          </p>
          <Link className="btn" href="/billing">
            Check billing
          </Link>
        </>
      ) : (
        <>
          <p className="lede">
            <span className="spin" /> Waiting for Stripe to confirm your credit pack. This
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

export default function CreditsSuccessPage() {
  // useSearchParams needs a Suspense boundary to prerender.
  return (
    <Suspense
      fallback={
        <p className="muted" role="status">
          Loading…
        </p>
      }
    >
      <CreditsSuccess />
    </Suspense>
  );
}
