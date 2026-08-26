"""
SQLAlchemy table definitions. Three tables:
  PaymentAttempt   — one row per failed payment in the batch
  AuditEvent       — one row per agent decision (multiple per payment)
  ComplianceState  — tracks retry count and cooldown per customer,
                     so the stopper knows where each customer stands

Keeping all models in one file since there are only three — no need
to split this until it gets much bigger.
"""

# implementation coming in Phase 0b
