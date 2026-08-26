"""
Given a diagnosed failure, decides what the recovery action should be
and when to do it.

Two layers here and it matters which is which:
- Timing + intervention type: deterministic rules. card_expired always
  needs a payment link, never a retry. insufficient_funds retries better
  around salary-credit windows. These aren't opinions, they're patterns.
- Message tone, channel, escalation: goes to Claude. A 3rd consecutive
  failure needs a different voice than a first-time one, and rules can't
  capture that nuance well.
"""

# implementation coming in Phase 3
