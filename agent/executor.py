"""
Does the actual recovery work — real Razorpay API calls where possible, explicitly
labeled simulation where human action (link payment, reviewer approval) is required.

Two public functions:
  execute_batch()              — synchronous actions for all DECIDED payments
  simulate_deferred_outcomes() — resolves EXECUTED payments that need human action

Retries get real Razorpay calls plus a seeded realism filter (test-mode always
succeeds; the filter gives realistic mixed outcomes per decline reason).
Payment links get real Razorpay link creation; outcome resolved in the second pass.
Human escalations are logged as queued; no automated API call is appropriate there.
"""

import os
import random
import time

import razorpay
from sqlalchemy.orm import Session

from audit.logger import log_event
from db.models import (
    AuditEvent, AuditLayer, InterventionType,
    PaymentAttempt, PaymentStatus,
)

# ASSUMPTION: per-reason retry success rates — documented estimates based on the
# timing rationale in decider.py (salary windows, 36h gap, etc.), not cited stats.
RETRY_SUCCESS_RATES: dict[str, float] = {
    "network_error":          0.95,  # transient fault; timing is not the barrier
    "insufficient_funds":     0.55,  # salary window helps; balance still uncertain
    "bank_declined":          0.45,  # 36h gap helps; some cases are structural
    "authentication_failed":  0.35,  # depends on whether customer re-authed in time
}

# ASSUMPTION: reasonable conversion estimates, not sourced from external data.
LINK_CONVERSION_RATE = 0.40  # fraction of sent payment links paid within 7 days
HUMAN_APPROVAL_RATE  = 0.60  # fraction of risk-flagged cases cleared by reviewer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_razorpay_client() -> razorpay.Client | None:
    """Return a configured Razorpay client, or None if credentials are missing."""
    key_id     = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return None
    return razorpay.Client(auth=(key_id, key_secret))


def _read_llm_message(payment: PaymentAttempt, db: Session) -> tuple[str, str]:
    """Pull channel and message text from the Phase 3 LLM audit event."""
    event = (
        db.query(AuditEvent)
        .filter_by(payment_id=payment.payment_id, step="decide", layer="llm")
        .first()
    )
    if not event:
        return "email", (
            f"Hi {payment.customer_name}, your payment of ₹{payment.amount // 100:,} "
            "could not be processed. Please update your payment details."
        )
    channel = "email"
    for part in event.decision.split():
        if part.startswith("channel="):
            channel = part.split("=", 1)[1]
    # reasoning format from decider: "{llm.reasoning}\n\nDraft: {llm.message}"
    message = event.reasoning.split("\n\nDraft: ", 1)[-1]
    return channel, message


def _log_message_send(
    payment: PaymentAttempt, db: Session, channel: str, message: str,
) -> None:
    """Record the outbound contact attempt with full message content."""
    log_event(
        db, payment.payment_id, "execute",
        f"{channel}_sent",
        message,
        AuditLayer.DETERMINISTIC.value,
        outcome="SIMULATED: no real SMS/email gateway",
    )


# ---------------------------------------------------------------------------
# Per-intervention-type execution
# ---------------------------------------------------------------------------

def _execute_retry(
    payment: PaymentAttempt, db: Session,
    rzp_client: razorpay.Client | None, rng: random.Random,
) -> None:
    """Call Razorpay then apply a realism filter — test-mode always returns success."""
    if rzp_client:
        try:
            # subscription.fetch confirms the record exists in Razorpay.
            # A production retry would call razorpay.subscription.charge() once
            # the SDK exposes it; fetch is the closest read-only proxy in test-mode.
            resp = rzp_client.subscription.fetch(payment.subscription_id)
            api_note = f"Razorpay subscription fetch: status={resp.get('status', 'unknown')}"
        except Exception as exc:
            # synthetic subscription_ids don't exist in test-mode — expected behavior
            api_note = f"Razorpay API: {exc} (synthetic ID; expected in test-mode)"
    else:
        api_note = "SIMULATED (no Razorpay credentials)"

    rate = RETRY_SUCCESS_RATES.get(payment.decline_reason, 0.40)
    recovered = rng.random() < rate
    payment.status = PaymentStatus.RECOVERED if recovered else PaymentStatus.UNRECOVERABLE
    outcome = "recovered" if recovered else "unrecoverable"
    log_event(
        db, payment.payment_id, "execute", outcome,
        f"{api_note}. ASSUMPTION: {payment.decline_reason} retries succeed at "
        f"{rate:.0%} given the retry timing logic. Seeded RNG → {outcome}.",
        AuditLayer.DETERMINISTIC.value, outcome=outcome,
    )


def _execute_payment_link(
    payment: PaymentAttempt, db: Session, rzp_client: razorpay.Client | None,
) -> None:
    """Create a Razorpay payment link so the customer can update their card."""
    if rzp_client:
        try:
            resp = rzp_client.payment_link.create({
                "amount":      payment.amount,
                "currency":    payment.currency,
                "description": f"Subscription renewal — {payment.subscription_id}",
                "customer":    {
                    "name":    payment.customer_name,
                    "email":   payment.customer_email,
                    "contact": payment.customer_phone or "",
                },
                "notify":    {"sms": bool(payment.customer_phone), "email": True},
                "expire_by": int(time.time()) + 7 * 24 * 3600,  # 7-day window
            })
            link_ref = resp.get("short_url") or resp.get("id", "unknown")
            api_note = f"Razorpay payment link created: {link_ref}"
        except Exception as exc:
            api_note = f"Razorpay API error: {exc}"
    else:
        api_note = "SIMULATED (no Razorpay credentials): would have created payment link"

    payment.status = PaymentStatus.EXECUTED
    log_event(
        db, payment.payment_id, "execute", "payment_link_sent",
        f"{api_note}. Awaiting customer action — outcome resolved by simulate_deferred_outcomes().",
        AuditLayer.DETERMINISTIC.value, outcome="executed_pending",
    )


def _execute_human_escalation(payment: PaymentAttempt, db: Session) -> None:
    """Queue the case for manual review. Automating here would bypass risk controls."""
    payment.status = PaymentStatus.EXECUTED
    log_event(
        db, payment.payment_id, "execute", "human_review_queued",
        f"risk_threshold case flagged for manual review. "
        f"ASSUMPTION: {HUMAN_APPROVAL_RATE:.0%} of such cases are cleared by a reviewer.",
        AuditLayer.DETERMINISTIC.value, outcome="executed_pending",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def execute_one(
    payment: PaymentAttempt, db: Session,
    rzp_client: razorpay.Client | None, rng: random.Random,
) -> None:
    """Route a single DECIDED payment to its execution path. Does not commit."""
    channel, message = _read_llm_message(payment, db)
    _log_message_send(payment, db, channel, message)

    it = payment.intervention_type
    if it == InterventionType.RETRY.value:
        _execute_retry(payment, db, rzp_client, rng)
    elif it == InterventionType.PAYMENT_LINK.value:
        _execute_payment_link(payment, db, rzp_client)
    elif it == InterventionType.HUMAN_ESCALATION.value:
        _execute_human_escalation(payment, db)


def execute_batch(db: Session) -> dict[str, int]:
    """Run the execution layer on all DECIDED payments. Single commit at the end."""
    rzp_client = _get_razorpay_client()
    rng = random.Random(42)
    payments = (
        db.query(PaymentAttempt)
        .filter(PaymentAttempt.status == PaymentStatus.DECIDED)
        .all()
    )
    counts: dict[str, int] = {}
    for payment in payments:
        execute_one(payment, db, rzp_client, rng)
        s = payment.status if isinstance(payment.status, str) else payment.status.value
        counts[s] = counts.get(s, 0) + 1
    db.commit()
    return counts


def simulate_deferred_outcomes(db: Session) -> dict[str, int]:
    """
    Resolve EXECUTED payments whose outcome depends on human action.

    All rates are ASSUMPTION-labeled — documented choices, not cited statistics.
    Uses seed=42 so the report is reproducible across runs.
    """
    rng = random.Random(42)
    payments = (
        db.query(PaymentAttempt)
        .filter(PaymentAttempt.status == PaymentStatus.EXECUTED)
        .all()
    )
    counts: dict[str, int] = {}
    for payment in payments:
        if payment.intervention_type == InterventionType.PAYMENT_LINK.value:
            rate   = LINK_CONVERSION_RATE
            reason = (
                f"SIMULATED: payment link follow-through. "
                f"ASSUMPTION: {rate:.0%} of sent links are paid within 7 days."
            )
        elif payment.intervention_type == InterventionType.HUMAN_ESCALATION.value:
            rate   = HUMAN_APPROVAL_RATE
            reason = (
                f"SIMULATED: human review outcome. "
                f"ASSUMPTION: {rate:.0%} of risk-flagged cases are cleared by a reviewer."
            )
        else:
            continue

        recovered = rng.random() < rate
        payment.status = PaymentStatus.RECOVERED if recovered else PaymentStatus.UNRECOVERABLE
        outcome = "recovered" if recovered else "unrecoverable"
        log_event(
            db, payment.payment_id, "execute", outcome,
            reason, AuditLayer.DETERMINISTIC.value, outcome=outcome,
        )
        counts[outcome] = counts.get(outcome, 0) + 1

    db.commit()
    return counts
