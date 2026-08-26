"""
Generates the end-of-batch summary after a recovery run finishes.

Shows what was recovered and what wasn't — broken down by decline reason
so it's obvious which failure types are hardest to recover from. The unresolved
section is intentional: a 100% recovery rate on synthetic data looks fake.
We want credible partial success, not a cherry-picked highlight reel.
"""

# implementation coming in Phase 7
