import type { ReactNode } from "react";

export type BannerTone = "info" | "warn" | "error";

/**
 * The banners carry the things a user most needs to be told: a payment that
 * failed, a subscription ending, a paywall they just hit. Every one of them
 * appears *after* an async call, so a sighted user sees it arrive and a screen
 * reader user was previously told nothing at all -- the markup was a plain div.
 *
 * The tone picks the live-region politeness, which is the whole reason this is
 * a component rather than eight copies of a className:
 *
 *   error -> role="alert"  (assertive: interrupts, because the action failed)
 *   warn  -> role="status" (polite: important, but the user is not blocked)
 *   info  -> role="status"
 *
 * Assertive is deliberately rare. Interrupting someone mid-sentence to tell
 * them about a discount is worse than waiting for a pause.
 */
export default function Banner({
  tone = "info",
  children,
}: {
  tone?: BannerTone;
  children: ReactNode;
}) {
  return (
    <div
      className={tone === "info" ? "banner" : `banner ${tone}`}
      role={tone === "error" ? "alert" : "status"}
    >
      {children}
    </div>
  );
}
