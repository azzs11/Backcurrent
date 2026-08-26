"""
Does the actual recovery work. Hits the Razorpay test-mode API to retry
a charge or create a payment link. Message sends (SMS/email) are simulated
but logged with full content — not hardcoded "success", never a fake result.

If an API call fails, that gets logged too. The audit trail shows real outcomes.
"""

# implementation coming in Phase 4
