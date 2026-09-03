import type { QuotaState } from "@/lib/types";
import { formatDateTime, formatLimit } from "@/lib/types";

/**
 * A quota, drawn from the entitlements payload rather than from any hard-coded
 * tier knowledge. An unlimited quota has no bar to draw -- a full-width meter
 * would imply a ceiling that does not exist.
 */
export default function QuotaMeter({ quota }: { quota: QuotaState }) {
  const unlimited = quota.limit === null;
  const ratio = unlimited ? 0 : Math.min(1, quota.used / Math.max(1, quota.limit ?? 1));
  const level = ratio >= 1 ? "full" : ratio >= 0.8 ? "high" : "";

  return (
    <div className="meter-row">
      <div className="meter-label">
        <span className="name">{quota.key}</span>
        <span className="value">
          {unlimited ? "unlimited" : `${quota.used.toLocaleString()} / ${formatLimit(quota.limit)}`}
        </span>
      </div>
      {!unlimited && (
        <div className="meter">
          <div className={`meter-fill ${level}`} style={{ width: `${ratio * 100}%` }} />
        </div>
      )}
      <div className="meter-foot">
        {unlimited ? "No cap on this plan" : `Resets ${formatDateTime(quota.reset_at)}`}
      </div>
    </div>
  );
}
