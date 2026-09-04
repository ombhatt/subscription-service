import type { Offer } from "@/lib/types";
import { formatAmount } from "@/lib/types";

/**
 * Renders whatever price a plan is being offered at.
 *
 * It takes an Offer rather than a number so that a discount — from a promotion
 * code today, from something that decides a price per user later — is a change
 * to the data, not to this component.
 */
export default function PriceTag({ offer }: { offer: Offer }) {
  if (offer.amount === null) return <span className="price-now">—</span>;

  const suffix = offer.interval === "annual" ? "/yr" : "/mo";

  return (
    <span className="price">
      {offer.compareAt !== null && (
        <span className="price-was">{formatAmount(offer.compareAt, offer.currency)}</span>
      )}
      <span className="price-now">
        {formatAmount(offer.amount, offer.currency)}
        {suffix}
      </span>
      {offer.label && <span className="price-label">{offer.label}</span>}
    </span>
  );
}
