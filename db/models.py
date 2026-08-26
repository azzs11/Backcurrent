"""
SQLAlchemy table definitions. Three tables:
  PaymentAttempt   — one row per failed payment in the batch
  AuditEvent       — one row per agent decision (multiple per payment)
  ComplianceState  — tracks retry count and cooldown per customer

Amount is stored in paise (integer) throughout — Razorpay's API works in paise
to avoid floating point issues, so we keep that convention end-to-end and
only convert to rupees at display time.
"""

import enum
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums
# Stored as plain strings in SQLite rather than using SQLAlchemy's Enum type.
# SQLAlchemy Enum bakes the values into the schema — adding a new decline reason
# mid-build would require a migration. String columns let us just add the value.
# Python enums still give us type safety where it matters.
# ---------------------------------------------------------------------------

class DeclineReason(str, enum.Enum):
    INSUFFICIENT_FUNDS   = "insufficient_funds"
    BANK_DECLINED        = "bank_declined"
    CARD_EXPIRED         = "card_expired"
    AUTHENTICATION_FAILED = "authentication_failed"
    RISK_THRESHOLD       = "risk_threshold"
    NETWORK_ERROR        = "network_error"
    MANDATE_REVOKED      = "mandate_revoked"


class PaymentStatus(str, enum.Enum):
    PENDING        = "pending"         # just loaded, not yet processed
    DIAGNOSED      = "diagnosed"       # diagnoser has run
    DECIDED        = "decided"         # decider has produced an intervention plan
    STOPPED        = "stopped"         # stopper halted before execution
    EXECUTED       = "executed"        # executor ran the action
    RECOVERED      = "recovered"       # payment actually went through
    UNRECOVERABLE  = "unrecoverable"   # permanently failed — stop trying


class InterventionType(str, enum.Enum):
    RETRY             = "retry"
    PAYMENT_LINK      = "payment_link"      # for card_expired and similar — can't just retry
    HUMAN_ESCALATION  = "human_escalation"  # risk_threshold cases with ambiguous signals
    NONE              = "none"              # opted-out or mandate_revoked — nothing to do


class AuditLayer(str, enum.Enum):
    DETERMINISTIC = "deterministic"
    LLM           = "llm"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

class PaymentAttempt(Base):
    """One row per failed payment in the batch."""

    __tablename__ = "payment_attempts"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    payment_id      = Column(String, unique=True, nullable=False, index=True)
    customer_id     = Column(String, nullable=False, index=True)
    customer_name   = Column(String, nullable=False)
    customer_email  = Column(String, nullable=False)
    customer_phone  = Column(String, nullable=True)
    subscription_id = Column(String, nullable=False)

    # paise, not rupees — divide by 100 when displaying
    amount   = Column(Integer, nullable=False)
    currency = Column(String,  default="INR", nullable=False)

    # failure_code is the raw string that came in (e.g. what Razorpay returned).
    # decline_reason is what the diagnoser classified it as after applying our rules.
    # They start the same. They diverge when we override — e.g. a raw "bank_declined"
    # that turns out to be card_expired once we check the card's expiry date.
    failure_code   = Column(String, nullable=False)
    decline_reason = Column(String, nullable=True)   # filled in by diagnoser

    # how many times this customer has already been attempted (across all payments,
    # not just this one — that's tracked in ComplianceState, mirrored here for
    # quick access without a join)
    attempt_number = Column(Integer, default=1, nullable=False)

    status         = Column(String, default=PaymentStatus.PENDING, nullable=False)
    opted_out      = Column(Boolean, default=False, nullable=False)

    intervention_type = Column(String, nullable=True)   # filled in by decider
    retry_at          = Column(DateTime, nullable=True)  # when decider says to retry

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    audit_events = relationship("AuditEvent", back_populates="payment", cascade="all, delete-orphan")


class AuditEvent(Base):
    """
    One row per agent decision. A single payment will have several of these —
    one for each step (diagnose, decide, execute, stop).

    The reasoning column is the important one — plain English explanation of
    what happened and why. This is what makes any case inspectable after the fact.
    """

    __tablename__ = "audit_events"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    payment_id = Column(String, ForeignKey("payment_attempts.payment_id"), nullable=False, index=True)

    # which step of the flow this event belongs to
    step     = Column(String, nullable=False)   # detect | diagnose | decide | execute | stop
    decision = Column(String, nullable=False)   # short label of what was decided
    reasoning = Column(Text,  nullable=False)   # why — written in plain English by whatever ran

    # explicit record of which layer made this call so judges can see exactly
    # where deterministic logic was used vs where Claude was involved
    layer = Column(String, nullable=False)   # deterministic | llm

    outcome    = Column(String, nullable=True)   # actual result after execution (not always known at decision time)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    payment = relationship("PaymentAttempt", back_populates="audit_events")


class ComplianceState(Base):
    """
    Tracks retry count and cooldown per customer — keyed by customer_id, not payment_id.
    The stopper reads this to know where a customer stands across the whole batch,
    not just on the current payment.

    opted_out is also stored on PaymentAttempt, but having it here means the stopper
    can check it without doing a join — one less query in the critical path.
    """

    __tablename__ = "compliance_state"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(String, unique=True, nullable=False, index=True)

    attempt_count     = Column(Integer,  default=0,     nullable=False)
    last_attempted_at = Column(DateTime, nullable=True)
    cooldown_until    = Column(DateTime, nullable=True)

    opted_out    = Column(Boolean,  default=False, nullable=False)
    opted_out_at = Column(DateTime, nullable=True)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
