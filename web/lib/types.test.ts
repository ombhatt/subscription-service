/**
 * The pure logic behind every price, discount and limit on screen.
 *
 * These functions had no direct test: they were reachable only by booting a
 * browser through Playwright, which is a slow and indirect way to find out
 * that a percentage was rounded the wrong way. They are pure and
 * deterministic, so they are the cheapest thing in the repo to cover properly.
 *
 * Assertions avoid pinning exact currency formatting. `Intl.NumberFormat` is
 * called with an `undefined` locale, so the symbol and separators depend on
 * the machine; what the code actually decides -- the arithmetic, the branch,
 * the suffix, the fraction digits -- is asserted instead.
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
  formatMoney,
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

  it("rounds a percentage to whole minor units", () => {
    // 1999 * 0.667 = 1333.333 -- a fractional cent would render as $13.3333
    const offer = offerFor(price(1999), "monthly", discount({ percent_off: 33.3 }));
    expect(Number.isInteger(offer.amount)).toBe(true);
    expect(offer.amount).toBe(1333);
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
    const offer = offerFor(undefined, "monthly");
    expect(offer.amount).toBeNull();
    expect(offer.currency).toBeNull();
    expect(offer.interval).toBe("monthly");
  });

  it("carries the interval through", () => {
    expect(offerFor(price(20000), "annual").interval).toBe("annual");
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
    expect(text).toContain("off on your first invoice");
    expect(text).toMatch(/5/);
  });

  it("falls back to a generic word rather than rendering 'null off'", () => {
    expect(describeDiscount(discount({ duration: "once" }))).toBe(
      "Discount on your first invoice",
    );
  });
});

// -------------------------------------------------------------- formatMoney

describe("formatMoney", () => {
  it("suffixes the billing period", () => {
    expect(formatMoney(price(2000), "monthly")).toMatch(/\/mo$/);
    expect(formatMoney(price(20000), "annual")).toMatch(/\/yr$/);
  });

  it("drops the trailing .00 on a whole amount", () => {
    const text = formatMoney(price(2000), "monthly");
    expect(text).not.toMatch(/[.,]00/);
    expect(text).toMatch(/20/);
  });

  it("keeps two decimals when the amount has cents", () => {
    expect(formatMoney(price(1999), "monthly")).toMatch(/19[.,]99/);
  });

  it("renders a dash rather than NaN when the price is missing", () => {
    expect(formatMoney(undefined, "monthly")).toBe("—");
    expect(formatMoney(price(null), "monthly")).toBe("—");
  });
});

// ------------------------------------------------------------- formatAmount

describe("formatAmount", () => {
  it("converts minor units to major", () => {
    expect(formatAmount(2000, "usd")).toMatch(/20/);
    expect(formatAmount(2000, "usd")).not.toMatch(/2000/);
  });

  it("defaults the currency instead of throwing on null", () => {
    // Intl throws RangeError on an invalid currency code, which would take the
    // whole billing page down over a missing field.
    expect(() => formatAmount(500, null)).not.toThrow();
  });

  it("keeps cents when the amount is not whole", () => {
    expect(formatAmount(1999, "usd")).toMatch(/19[.,]99/);
  });

  it("formats zero", () => {
    expect(formatAmount(0, "usd")).toMatch(/0/);
  });
});

// -------------------------------------------------------- limits and dates

describe("formatLimit", () => {
  it("renders an unlimited quota as a word, not as null", () => {
    expect(formatLimit(null)).toBe("Unlimited");
  });

  it("groups large numbers", () => {
    expect(formatLimit(1500)).toMatch(/1[.,\s ]?500/);
  });

  it("renders zero as zero, not as unlimited", () => {
    // `limit || "Unlimited"` would be wrong here and is an easy mistake.
    expect(formatLimit(0)).toBe("0");
  });
});

describe("formatDate / formatDateTime", () => {
  it("renders a dash for a missing date", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDateTime(null)).toBe("—");
  });

  it("renders a real date", () => {
    const text = formatDate("2026-10-04T20:02:22Z");
    expect(text).toMatch(/2026/);
    expect(text).not.toMatch(/Invalid/);
  });

  it("includes a time of day in the datetime variant", () => {
    const text = formatDateTime("2026-10-04T20:02:22Z");
    expect(text).toMatch(/\d{1,2}:\d{2}/);
  });
});
