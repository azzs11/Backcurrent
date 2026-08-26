"""
Structured event logging to SQLite. One AuditEvent row per agent decision.

Each step in the pipeline (diagnose, decide, execute, stop) calls log_event once.
The reasoning column is the one that matters for demo inspection — it should read
like a developer explaining a non-obvious constraint, not a restatement of the code.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from db.models import AuditEvent


def log_event(
    db: Session,
    payment_id: str,
    step: str,
    decision: str,
    reasoning: str,
    layer: str,
    outcome: str | None = None,
) -> AuditEvent:
    """Write one agent decision to the audit trail and return the created row."""
    event = AuditEvent(
        payment_id = payment_id,
        step       = step,
        decision   = decision,
        reasoning  = reasoning,
        layer      = layer,
        outcome    = outcome,
        created_at = datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(event)
    # caller controls the transaction — don't commit here, let the batch loop do it
    return event
