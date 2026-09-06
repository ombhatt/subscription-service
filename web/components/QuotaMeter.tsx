import type { QuotaState } from "@/lib/types";
import { formatDateTime, formatLimit, humanizeKey } from "@/lib/types";

/**
 * A quota, drawn from the entitlements payload rather than from any hard-coded
 * tier knowledge. An unlimited quota has no bar to draw -- a full-width meter
 * would imply a ceiling that does not exist.
 *
 * The bar was previously a bare div whose only output was a CSS width, so it
 * conveyed nothing to anyone not looking at it, and "nearly out" was carried by
 * colour alone -- invisible to a screen reader and to roughly one man in twelve
 * looking straight at it. Now it is a progressbar with real values, and the
 * near-limit state is stated in words next to the colour rather than by it.
 */
export default function QuotaMeter({ quota }: { quota: QuotaState }) {
  const unlimited = quota.limit === null;
  const limit = quota.limit ?? 1;
  const ratio = unlimited ? 0 : Math.min(1, quota.used / Math.max(1, limit));
  const level = ratio >= 1 ? "full" : ratio >= 0.8 ? "high" : "";
  const label = humanizeKey(quota.key);

  // What a screen reader should say instead of "seventy-five percent", which is
  // true and useless: the numbers the user actually needs are the count, the
  // cap and when it lifts.
  const valueText = unlimited
    ? `${label}: unlimited`
    : `${quota.used.toLocaleString()} of ${formatLimit(quota.limit)} used`;

  const note = level === "full" ? "None left" : level === "high" ? "Nearly used up" : null;

  return (
    <div className="meter-row">
      <div className="meter-label">
        <span className="name">{label}</span>
        <span className="value">
          {unlimited ? "unlimited" : `${quota.used.toLocaleString()} / ${formatLimit(quota.limit)}`}
        </span>
      </div>
      {!unlimited && (
        <div
          className="meter"
          role="progressbar"
          aria-label={label}
          aria-valuemin={0}
          aria-valuemax={limit}
          aria-valuenow={Math.min(quota.used, limit)}
          aria-valuetext={valueText}
        >
          <div className={level ? `meter-fill ${level}` : "meter-fill"} style={{ width: `${ratio * 100}%` }} />
        </div>
      )}
      <div className="meter-foot">
        {note && <strong className={`meter-note ${level}`}>{note}. </strong>}
        {unlimited ? "No cap on this plan" : `Resets ${formatDateTime(quota.reset_at)}`}
      </div>
    </div>
  );
}
