"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { ApiError, getEntitlements, getPlans, sendChat } from "@/lib/api";
import type { ChatReply, Entitlements, FeatureNotEntitled, Plan, QuotaExceeded } from "@/lib/types";
import { formatDateTime } from "@/lib/types";
import { useRequireSession } from "@/lib/user";

interface Turn {
  role: "me" | "them";
  text: string;
}

/**
 * A stand-in product surface, here to show the two ways the paywall appears:
 * a model that is not on your tier (403), and running out of messages (429).
 *
 * Both errors carry everything needed to render the upsell -- which tier lifts
 * the limit, and when it resets -- so the frontend never has to know what a
 * tier contains.
 */
export default function ChatPage() {
  const { userId, ready } = useRequireSession();
  const [ents, setEnts] = useState<Entitlements | null>(null);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [model, setModel] = useState("small");
  const [draft, setDraft] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [blocked, setBlocked] = useState<QuotaExceeded | FeatureNotEntitled | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  useEffect(() => {
    if (!ready) return;
    Promise.all([getEntitlements(), getPlans()])
      .then(([e, p]) => {
        setEnts(e);
        setPlans(p);
      })
      .catch((err: Error) => setError(err.message));
  }, [userId, ready]);

  // Every model the product offers, not just the ones this tier can use --
  // locked options have to be visible for the paywall to mean anything.
  const allModels = useMemo(() => {
    const seen: string[] = [];
    for (const plan of plans) {
      for (const name of plan.features.models ?? []) {
        if (!seen.includes(name)) seen.push(name);
      }
    }
    return seen;
  }, [plans]);

  const unlocked = ents?.features.models ?? [];
  const messageQuota = ents?.quotas.find((q) => q.key === "messages_per_day");

  async function send(event: React.FormEvent) {
    event.preventDefault();
    if (!draft.trim()) return;

    const text = draft.trim();
    setSending(true);
    setBlocked(null);
    setError(null);

    try {
      const reply: ChatReply = await sendChat(model, text);
      setTurns((prev) => [...prev, { role: "me", text }, { role: "them", text: reply.reply }]);
      setDraft("");
      // Reflect the consumed message without a second round trip.
      setEnts((prev) =>
        prev
          ? {
              ...prev,
              quotas: prev.quotas.map((q) =>
                q.key === reply.quota.key
                  ? { ...q, used: reply.quota.used, remaining: reply.quota.remaining }
                  : q,
              ),
            }
          : prev,
      );
    } catch (err) {
      if (err instanceof ApiError && (err.status === 403 || err.status === 429)) {
        setBlocked(err.body as QuotaExceeded | FeatureNotEntitled);
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSending(false);
    }
  }

  if (!ready) return <p className="muted">Loading…</p>;

  return (
    <>
      <h1>Chat</h1>
      <p className="lede">
        A metered endpoint. It checks the model against your tier, then consumes a message from
        your daily quota — the same two checks any real endpoint would make.
      </p>

      {error && (
        <div className="banner error">
          <strong>Request failed.</strong> {error}
        </div>
      )}

      {blocked?.error === "feature_not_entitled" && (
        <div className="banner warn">
          <div className="row">
            <span>
              <strong>{blocked.feature.replace("model:", "")}</strong> is not on the{" "}
              {blocked.current_tier} plan
              {blocked.required_tier ? <> — it needs {blocked.required_tier}</> : null}.
            </span>
            <Link className="btn" href="/">
              See plans
            </Link>
          </div>
        </div>
      )}

      {blocked?.error === "quota_exceeded" && (
        <div className="banner warn">
          <div className="row">
            <span>
              <strong>Daily limit reached.</strong> You have used all{" "}
              {blocked.limit.toLocaleString()} messages. Resets{" "}
              {formatDateTime(blocked.reset_at)}
              {blocked.upgrade_tier ? <>, or move to {blocked.upgrade_tier} for more.</> : "."}
            </span>
            {blocked.upgrade_tier && (
              <Link className="btn" href="/">
                Upgrade
              </Link>
            )}
          </div>
        </div>
      )}

      <div className="card">
        {turns.length > 0 && (
          <div className="chat-log">
            {turns.map((turn, index) => (
              <div key={index} className={`bubble ${turn.role}`}>
                {turn.text}
              </div>
            ))}
          </div>
        )}

        <form onSubmit={send} className="inline">
          <select value={model} onChange={(event) => setModel(event.target.value)}>
            {allModels.map((name) => {
              const locked = !unlocked.includes(name);
              return (
                <option key={name} value={name}>
                  {name}
                  {locked ? " (locked)" : ""}
                </option>
              );
            })}
          </select>
          <input
            style={{ flex: 1, minWidth: 200 }}
            value={draft}
            placeholder="Say something…"
            onChange={(event) => setDraft(event.target.value)}
          />
          <button className="primary" type="submit" disabled={sending || !draft.trim()}>
            {sending ? "Sending…" : "Send"}
          </button>
        </form>

        {messageQuota && (
          <p className="muted" style={{ marginTop: 12, marginBottom: 0 }}>
            {messageQuota.remaining === null
              ? "Unlimited messages on this plan."
              : `${messageQuota.remaining.toLocaleString()} of ${messageQuota.limit?.toLocaleString()} messages left today.`}{" "}
            Pick a locked model to see the other kind of paywall.
          </p>
        )}
      </div>
    </>
  );
}
