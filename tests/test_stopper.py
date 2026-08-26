"""
Tests for the stopping rules. Three scenarios that all need to work:
- opt-out customer: should halt before any other check runs
- 4th attempt on a max-3 policy: should be blocked
- retry attempted inside the cooldown window: should be blocked

Order matters too — opt-out should always win over everything else.
"""

# tests coming in Phase 5
