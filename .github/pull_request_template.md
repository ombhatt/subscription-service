## What changed

<!-- One or two sentences. What does this do that the code did not do before? -->

## Why

<!-- The reason, not the mechanism. Link the issue if there is one. -->

## How it was verified

<!-- Tests are the default answer. Say which, and what they assert. If you
     checked something by hand (a real Stripe test-mode payment, a Test Clock
     run), say so and say what you saw. -->

## Billing checklist

<!-- Delete any line that does not apply. These are the things that have
     actually gone wrong in subscription systems. -->

- [ ] Nothing outside `app/plans.py` hard-codes a tier name or a numeric limit
- [ ] Access is still granted on the webhook, never on the checkout redirect
- [ ] New webhook handling goes through `sync_subscription_from_stripe` rather
      than applying an event payload as a delta
- [ ] Duplicate and out-of-order delivery of any new event type is covered
- [ ] Schema changes have a migration, and the migration's `downgrade()` works
- [ ] Entitlement-affecting writes invalidate the entitlement cache
- [ ] No secrets, price ids, or customer data in the diff
