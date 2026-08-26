"""
Takes a failed payment and works out the specific reason it declined.
The goal is to get past the generic "payment failed" and nail down something
like insufficient_funds or card_expired — because the right next move depends
entirely on which one it is.

All logic here is deterministic. No LLM involved — decline reasons are facts,
not judgment calls.
"""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from audit.logger import log_event
from db.models import AuditLayer, DeclineReason, PaymentAttempt, PaymentStatus


def _is_card_expired(card: dict) -> bool:
    """Return True if the card's expiry month/year is before the current month."""
    exp_month = card.get("expiry_month")
    exp_year  = card.get("expiry_year")
    if not exp_month or not exp_year:
        return False
    today = datetime.now(timezone.utc)
    # cards expire at end of month, so (2026, 7) expired once August 2026 started
    return (int(exp_year), int(exp_month)) < (today.year, today.month)


def _classify(failure_code: str, metadata: dict) -> tuple[DeclineReason, str]:
    """
    Map failure_code + raw metadata to a specific DeclineReason.
    Returns (reason, reasoning_text) — the reasoning is written for the audit trail,
    not for the code reader, so it explains what was found in the data.
    """
    if failure_code == "bank_declined":
        # bank_declined is the most ambiguous code — it can hide card_expired or
        # authentication_failed underneath. Real banks frequently return DO_NOT_HONOR
        # regardless of the actual cause; we have to dig into the metadata.
        card = metadata.get("card", {})
        if card and _is_card_expired(card):
            exp = f"{card.get('expiry_month')}/{card.get('expiry_year')}"
            return (
                DeclineReason.CARD_EXPIRED,
                f"failure_code was bank_declined but card.expiry={exp} is in the past — "
                f"bank's DO_NOT_HONOR hid the real reason. Override: card_expired.",
            )

        if metadata.get("auth_type"):
            auth_err = metadata.get("error_code", "unknown")
            return (
                DeclineReason.AUTHENTICATION_FAILED,
                f"failure_code was bank_declined but auth_type='{metadata['auth_type']}' "
                f"and error_code='{auth_err}' signal the customer didn't complete authentication. "
                f"Override: authentication_failed.",
            )

        # nothing in the metadata to override — genuine generic bank rejection
        bank = metadata.get("bank_name", "unknown bank")
        return (
            DeclineReason.BANK_DECLINED,
            f"failure_code bank_declined with no card expiry or auth signal from {bank}. "
            f"No override possible — classifying as bank_declined.",
        )

    if failure_code == "insufficient_funds":
        bank = metadata.get("bank_name", "unknown bank")
        return (
            DeclineReason.INSUFFICIENT_FUNDS,
            f"Bank explicitly reported insufficient balance ({bank}). Direct match.",
        )

    if failure_code == "card_expired":
        card = metadata.get("card", {})
        exp  = f"{card.get('expiry_month')}/{card.get('expiry_year')}" if card else "unknown"
        return (
            DeclineReason.CARD_EXPIRED,
            f"Bank returned card_expired directly. Card expiry: {exp}. Direct match.",
        )

    if failure_code == "authentication_failed":
        auth_err = metadata.get("error_code", "unknown")
        return (
            DeclineReason.AUTHENTICATION_FAILED,
            f"Bank returned authentication_failed (error_code={auth_err}). Direct match.",
        )

    if failure_code == "risk_threshold":
        flags = metadata.get("risk_flags", [])
        score = metadata.get("risk_score", "n/a")
        return (
            DeclineReason.RISK_THRESHOLD,
            f"Risk engine blocked payment. score={score}, flags={flags}. "
            f"Needs human review — rule engine can't resolve ambiguous fraud signals.",
        )

    if failure_code == "network_error":
        # error_source="gateway" distinguishes infrastructure timeouts from bank rejections;
        # network errors are transient and often resolve within minutes without any
        # customer action required
        src = metadata.get("error_source", "unknown")
        return (
            DeclineReason.NETWORK_ERROR,
            f"Gateway-side timeout (error_source={src}). Transient — no bank or customer "
            f"action caused this. Safe to retry almost immediately.",
        )

    if failure_code == "mandate_revoked":
        reason = metadata.get("revocation_reason", "unknown")
        return (
            DeclineReason.MANDATE_REVOKED,
            f"Customer revoked the mandate (reason={reason}). Cannot retry — "
            f"customer must set up a new mandate to resume payments.",
        )

    # unknown code from a bank we haven't seen before — treat as generic decline
    return (
        DeclineReason.BANK_DECLINED,
        f"Unrecognised failure_code '{failure_code}'. Defaulting to bank_declined.",
    )


def diagnose_one(payment: PaymentAttempt, db: Session) -> DeclineReason:
    """Classify one payment and write an audit event. Does not commit."""
    metadata = json.loads(payment.raw_metadata or "{}")
    reason, reasoning = _classify(payment.failure_code, metadata)

    payment.decline_reason = reason.value
    payment.status         = PaymentStatus.DIAGNOSED

    log_event(
        db         = db,
        payment_id = payment.payment_id,
        step       = "diagnose",
        decision   = reason.value,
        reasoning  = reasoning,
        layer      = AuditLayer.DETERMINISTIC.value,
    )

    return reason


def diagnose_batch(db: Session) -> dict[str, int]:
    """
    Run diagnosis on all pending payments. Returns a count by decline reason.
    Commits after processing the full batch — partial commits would leave the
    audit trail inconsistent if the process is interrupted mid-batch.
    """
    payments = (
        db.query(PaymentAttempt)
        .filter(PaymentAttempt.status == PaymentStatus.PENDING)
        .all()
    )

    counts: dict[str, int] = {}
    for payment in payments:
        reason = diagnose_one(payment, db)
        counts[reason.value] = counts.get(reason.value, 0) + 1

    db.commit()
    return counts
