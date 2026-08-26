"""
Tests for the diagnoser. A few tricky cases to watch out for:
- card_expired sometimes comes back from the bank as bank_declined, so
  we need to make sure the expiry date check overrides the error code
- mandate_revoked should never be re-classified as anything recoverable
- the same raw error code can mean different things depending on context
"""

# tests coming in Phase 2
