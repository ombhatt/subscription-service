/**
 * The pure logic behind every price, discount and limit on screen.
 *
 * These functions had no direct test: they were reachable only by booting a
 * browser through Playwright, which is a slow and indirect way to find out
 * that a percentage was rounded the wrong way. They are pure and
 * deterministic, so they are the cheapest thing in the repo to cover properly.
 *
 * The app formats with the viewer's locale and time zone, so vitest.config.mts
 * pins both to what Playwright uses (en-US, UTC) and these tests assert exact
 * strings. Without that, "$500 off" passes for "$5 off" and a date a day late
 * passes for 2026.
 */

import { describe, expect, it } from "vitest";

import {
  type Discount,
  type Price,
  describeDiscount,
  formatAmount,
  formatDate,
  formatDateTime,
  formatLimit,
  humanizeKey,
  offerFor,
} from "./types";

const price = (unit_amount: number | null, currency: string | null = "usd"): Price => ({
  price_id: "price_x",
  unit_amount,
  currency,
});

const discount = (over: Partial<Discount>): Discount => ({
  coupon_id: "co_1",
  name: null,
  percent_off: null,
  amount_off: null,
  currency: "usd",
  duration: "once",
  duration_in_months: null,
  promotion_code: "promo_1",
  ends_at: null,
  ...over,
});

// ---------------------------------------------------------------- offerFor

describe("offerFor", () => {
  it("passes the list price through when there is no discount", () => {
    const offer = offerFor(price(2000), "monthly");
    expect(offer.amount).toBe(2000);
    expect(offer.compareAt).toBeNull();
    expect(offer.label).toBeNull();
  });

  it("applies a percentage and keeps the original as compareAt", () => {
    const offer = offerFor(price(2000), "monthly", discount({ percent_off: 25 }));
    expect(offer.amount).toBe(1500);
    expect(offer.compareAt).toBe(2000);
    expect(offer.label).toContain("25% off");
  });

  it("rounds a percentage to the nearest whole minor unit", () => {
    // 1999 * 0.667 = 1333.333 and 1999 * 0.4 = 799.6: one rounds down, one up.
    expect(offerFor(price(1999), "monthly", discount({ percent_off: 33.3 })).amount).toBe(1333);
    expect(offerFor(price(1999), "monthly", discount({ percent_off: 60 })).amount).toBe(800);
  });

  it("subtracts a fixed amount", () => {
    const offer = offerFor(price(2000), "monthly", discount({ amount_off: 500 }));
    expect(offer.amount).toBe(1500);
    expect(offer.compareAt).toBe(2000);
  });

  it("never renders a negative price when the coupon exceeds the price", () => {
    const offer = offerFor(price(300), "monthly", discount({ amount_off: 500 }));
    expect(offer.amount).toBe(0);
  });

  it("treats a 100% coupon as free, not as absent", () => {
    const offer = offerFor(price(2000), "monthly", discount({ percent_off: 100 }));
    expect(offer.amount).toBe(0);
    expect(offer.compareAt).toBe(2000);
  });

  it("degrades to the list price when Stripe gave us no amount", () => {
    // The pricing page must still render during a Stripe outage.
    const offer = offerFor(price(null), "monthly", discount({ percent_off: 25 }));
    expect(offer.amount).toBeNull();
    expect(offer.label).toBeNull();
  });

  it("returns the base offer for a discount that discounts nothing", () => {
    const offer = offerFor(price(2000), "monthly", discount({}));
    expect(offer.amount).toBe(2000);
    expect(offer.compareAt).toBeNull();
  });

  it("survives Stripe returning no price object at all", () => {
    // The pricing page renders limits from config and amounts from Stripe; a
    // Stripe outage means no price object, and the page must still draw.
    const offer = offerFor(undefined, "annual");
    expect(offer.amount).toBeNull();
    expect(offer.currency).toBeNull();
    expect(offer.interval).toBe("annual");
  });
});

// --------------------------------------------------------- describeDiscount

describe("describeDiscount", () => {
  it("describes a forever coupon", () => {
    const text = describeDiscount(discount({ percent_off: 20, duration: "forever" }));
    expect(text).toBe("20% off, for as long as you subscribe");
  });

  it("describes a one-off coupon as applying to the first invoice", () => {
    const text = describeDiscount(discount({ percent_off: 20, duration: "once" }));
    expect(text).toBe("20% off on your first invoice");
  });

  it("describes a repeating coupon with its length", () => {
    const text = describeDiscount(
      discount({ percent_off: 20, duration: "repeating", duration_in_months: 3 }),
    );
    expect(text).toBe("20% off for 3 months");
  });

  it("omits the length when repeating without a month count", () => {
    const text = describeDiscount(discount({ percent_off: 20, duration: "repeating" }));
    expect(text).toBe("20% off");
  });

  it("describes a fixed-amount coupon", () => {
    const text = describeDiscount(discount({ amount_off: 500, duration: "once" }));
    expect(text).toBe("$5 off on your first invoice");
  });

  it("falls back to a generic word rather than rendering 'null off'", () => {
    expect(describeDiscount(discount({ duration: "once" }))).toBe(
      "Discount on your first invoice",
    );
  });
});

// ------------------------------------------------------------- formatAmount

describe("formatAmount", () => {
  it("converts minor units to major and drops a trailing .00", () => {
    expect(formatAmount(2000, "usd")).toBe("$20");
  });

  it("defaults the currency to dollars instead of throwing on null", () => {
    // Intl throws RangeError on an invalid currency code, which would take the
    // whole billing page down over a missing field.
    expect(formatAmount(500, null)).toBe("$5");
  });

  it("keeps cents when the amount is not whole", () => {
    expect(formatAmount(1999, "usd")).toBe("$19.99");
  });

  it("formats zero", () => {
    expect(formatAmount(0, "usd")).toBe("$0");
  });
});

// -------------------------------------------------------- limits and dates

describe("formatLimit", () => {
  it("renders an unlimited quota as a word, not as null", () => {
    expect(formatLimit(null)).toBe("Unlimited");
  });

  it("groups large numbers", () => {
    expect(formatLimit(1500)).toBe("1,500");
  });

  it("renders zero as zero, not as unlimited", () => {
    // `limit || "Unlimited"` would be wrong here and is an easy mistake.
    expect(formatLimit(0)).toBe("0");
  });
});

describe("humanizeKey", () => {
  it("turns a quota key into something readable", () => {
    expect(humanizeKey("messages_per_day")).toBe("Messages per day");
  });

  it("turns a feature key into something readable", () => {
    expect(humanizeKey("history_retention_days")).toBe("History retention days");
  });

  it("leaves a single word alone but capitalised", () => {
    expect(humanizeKey("models")).toBe("Models");
  });

  it("survives an empty key rather than throwing", () => {
    // A malformed entitlements payload must not take the billing page down.
    expect(humanizeKey("")).toBe("");
  });
});

describe("formatDate / formatDateTime", () => {
  it("renders a dash for a missing date", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDateTime(null)).toBe("—");
  });

  it("renders a real date", () => {
    expect(formatDate("2026-10-04T20:02:22Z")).toBe("Oct 4, 2026");
  });

  it("includes a time of day in the datetime variant", () => {
    expect(formatDateTime("2026-10-04T20:02:22Z")).toBe("Oct 4, 8:02 PM");
  });
});
