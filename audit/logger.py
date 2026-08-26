"""
Writes a structured event row to SQLite every time the agent makes a decision.
Covers all four steps: detect, diagnose, decide, execute.

This isn't just logging for debugging — it's a first-class feature. The whole
point is that any payment can be fully reconstructed after the fact: what was
decided, why, and what actually happened.
"""

# implementation coming in Phase 6
