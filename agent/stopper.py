"""
Compliance guardrails. This runs before the decider does anything else.

Three checks, in this order:
1. Has the customer opted out? If yes, halt immediately. No cooldown, no retry,
   just stop. This isn't a preference — it's a hard regulatory requirement.
2. Have we hit the max retry limit? Three strikes is the industry standard
   before escalating to human collections.
3. Are we inside the cooldown window for this decline reason? Different reasons
   have different windows — network errors clear in hours, insufficient_funds
   might need days.

All three are deterministic. Compliance rules shouldn't be LLM judgment calls.
"""

# implementation coming in Phase 5
