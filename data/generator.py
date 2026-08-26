"""
Generates a realistic batch of ~250-300 failed subscription payments.

A few things matter here that most generators get wrong:
- failure reasons need to be skewed, not evenly distributed. insufficient_funds
  dominates in the real world; network_error is genuinely rare.
- amounts should look like real subscription prices (₹847, ₹1299) not round numbers.
- some customers should already be on their 2nd or 3rd failure — that's what
  exercises the stopping rules during the demo.
- a handful of cases should be genuinely unrecoverable (chronic failures, opt-outs)
  so the report's "unresolved" section has real content.
"""

# implementation coming in Phase 1
