"""
Takes a failed payment and works out the specific reason it declined.
The goal is to get past the generic "payment failed" and nail down something
like insufficient_funds or card_expired — because the right next move depends
entirely on which one it is.

All logic here is deterministic. No LLM involved — decline reasons are facts,
not judgment calls.
"""

# implementation coming in Phase 2
