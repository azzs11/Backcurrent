"""
Tests for the decider. Split into two parts:
- The timing/intervention logic is fully testable without any mocking.
  If the reason is card_expired, the decision should always be "payment link,
  not retry" — that's a fact, not an opinion.
- The LLM layer uses a mock so these tests don't hit the Claude API.
  We're testing that the right prompt gets built and the response gets
  parsed correctly, not what Claude says.
"""

# tests coming in Phase 3
