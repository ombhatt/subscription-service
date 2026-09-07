"use client";

import { useId, useState } from "react";

import Banner from "@/components/Banner";
import { ApiError, contactSales } from "@/lib/api";
import type { ContactSalesPayload } from "@/lib/types";

/**
 * The Enterprise inquiry form.
 *
 * Enterprise has no price and no checkout: the conversion event on this page is
 * someone telling us how to reach them. So this is the thing worth measuring,
 * and the backend records every submission with the tier of whoever sent it.
 *
 * Only the email is required. Every extra field is a chance to abandon the
 * form, and a reply-to address is enough to start the conversation -- seats and
 * message are asked for because they are genuinely useful (seats especially:
 * it is the evidence for whether Enterprise eventually means a bigger plan or
 * a team product) but never demanded.
 */
export default function ContactSales({
  source,
  onDone,
}: {
  source: ContactSalesPayload["source"];
  onDone?: () => void;
}) {
  // The submit is deliberately not called "Contact sales": that is the name of
  // the button that opens this form, and two controls sharing an accessible
  // name on one page is ambiguous to anyone navigating by name rather than by
  // sight.
  const id = useId();
  const [email, setEmail] = useState("");
  const [company, setCompany] = useState("");
  const [seats, setSeats] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await contactSales({
        email: email.trim(),
        company: company.trim() || null,
        seats: seats ? Number(seats) : null,
        message: message.trim() || null,
        source,
      });
      setSent(true);
      onDone?.();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (sent) {
    return (
      <Banner>
        <strong>Thanks — we&apos;ll be in touch.</strong> We have your details and someone
        will reply to <strong>{email}</strong>.
      </Banner>
    );
  }

  return (
    <div className="card" style={{ maxWidth: 520 }}>
      <h2 style={{ marginTop: 0 }}>Talk to us about Enterprise</h2>
      <p className="muted">
        Custom limits, invoicing and a contract. Tell us how to reach you and we&apos;ll
        take it from there.
      </p>

      {error && (
        <Banner tone="error">
          <strong>That didn&apos;t send.</strong> {error}
        </Banner>
      )}

      <form onSubmit={submit} className="stack" style={{ gap: 12 }}>
        <label className="field" htmlFor={`${id}-email`}>
          <span>Work email</span>
          <input
            id={`${id}-email`}
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </label>

        <label className="field" htmlFor={`${id}-company`}>
          <span>Company (optional)</span>
          <input
            id={`${id}-company`}
            autoComplete="organization"
            value={company}
            onChange={(event) => setCompany(event.target.value)}
          />
        </label>

        <label className="field" htmlFor={`${id}-seats`}>
          <span>How many people (optional)</span>
          <input
            id={`${id}-seats`}
            type="number"
            min={1}
            inputMode="numeric"
            value={seats}
            onChange={(event) => setSeats(event.target.value)}
          />
        </label>

        <label className="field" htmlFor={`${id}-message`}>
          <span>Anything we should know (optional)</span>
          <textarea
            id={`${id}-message`}
            rows={3}
            maxLength={2000}
            value={message}
            onChange={(event) => setMessage(event.target.value)}
          />
        </label>

        <button className="primary" type="submit" disabled={busy || !email.trim()} aria-busy={busy}>
          {busy ? "Sending…" : "Send request"}
        </button>
      </form>
    </div>
  );
}
