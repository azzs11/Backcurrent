"""
All the API endpoints in one place:
  POST /batch/run       — kick off a recovery run on the current payment batch
  GET  /audit/{id}      — pull the full decision trail for a single payment
  GET  /report          — end-of-batch summary with recovery stats

Keeping all routes in one file for now — if this grows it'll need splitting,
but we're not there yet.
"""

# implementation coming in Phase 0c, then expanded in later phases
