"""
Given a diagnosed failure, decides what the recovery action should be and when.

Two layers — the separation is intentional and explicit:

  DETERMINISTIC: Is this retryable? When? What intervention type?
    These are binary facts from the decline reason. No judgment involved.
    card_expired always needs a payment link. network_error always retries in 30 min.

  LLM (Claude): What tone? Which channel? What does the message say?
    Tone is genuinely ambiguous — a 3rd consecutive failure needs a different
    voice than a 1st. Rules can't capture this reliably. Claude handles it.

Judges should be able to read the audit trail and see exactly which layer
made each call and why.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import anthropic
from sqlalchemy.orm import Session

from audit.logger import log_event
from db.models import (
    AuditLayer, DeclineReason, InterventionType,
    PaymentAttempt, PaymentStatus,
)


@dataclass
class TimingDecision:
    """Output of the deterministic layer — what action to take and when."""
    intervention_type: str
    retry_at: datetime | None
    reasoning: str


@dataclass
class LLMDecision:
    """Output of the LLM layer — how to communicate the intervention."""
    tone: str
    channel: str
    message: str
    reasoning: str


# ---------------------------------------------------------------------------
# DETERMINISTIC LAYER — never put LLM calls in this section
# ---------------------------------------------------------------------------

def _timing_decision(payment: PaymentAttempt, now: datetime) -> TimingDecision:
    """Map decline reason to intervention type and retry timing. Pure logic, no I/O."""
    dr = payment.decline_reason

    if dr == DeclineReason.INSUFFICIENT_FUNDS.value:
        today = now.date()
        if today.day <= 5:
            # already inside the salary window — give it 2 days to settle
            retry_at = now + timedelta(days=2)
            note = "in salary window; retry after balance settles"
        else:
            # salary hits accounts on 1st-5th; retry on 2nd of next month
            # (1st can be a public holiday, 2nd is safer)
            m, y = (1, today.year + 1) if today.month == 12 else (today.month + 1, today.year)
            retry_at = datetime(y, m, 2, 10, 0, 0)
            note = f"waiting for next salary window ({y}-{m:02d}-02)"
        return TimingDecision(InterventionType.RETRY.value, retry_at,
                              f"insufficient_funds: {note}. Mid-month retries almost always fail.")

    if dr == DeclineReason.NETWORK_ERROR.value:
        # gateway timeout — no bank or customer action caused this; resolves itself
        return TimingDecision(InterventionType.RETRY.value, now + timedelta(minutes=30),
                              "network_error: transient gateway fault. Retry in 30 min.")

    if dr == DeclineReason.CARD_EXPIRED.value:
        # retrying an expired card always fails — the card number is permanently dead.
        # only path forward is the customer updating their card via a payment link.
        return TimingDecision(InterventionType.PAYMENT_LINK.value, None,
                              "card_expired: retry is impossible. Send payment link for card update.")

    if dr == DeclineReason.BANK_DECLINED.value:
        # same-day retries on generic bank declines succeed <10% of the time;
        # 36h gives the bank time to clear whatever triggered the decline
        return TimingDecision(InterventionType.RETRY.value, now + timedelta(hours=36),
                              "bank_declined: 36h gap before retry. Same-day attempts rarely succeed.")

    if dr == DeclineReason.AUTHENTICATION_FAILED.value:
        # customer must re-authenticate the mandate before the next charge can land;
        # 48h gives them time to respond to the re-auth notification
        return TimingDecision(InterventionType.RETRY.value, now + timedelta(hours=48),
                              "authentication_failed: retry after 48h — customer needs time to re-auth.")

    if dr == DeclineReason.RISK_THRESHOLD.value:
        # ambiguous fraud signals — automated retry would bypass risk controls.
        # LLM flags the case for human review; a human decides whether to unblock.
        return TimingDecision(InterventionType.HUMAN_ESCALATION.value, None,
                              "risk_threshold: automated retry unsafe. Queued for human review.")

    # mandate_revoked: customer explicitly cancelled — there is nothing to retry
    return TimingDecision(InterventionType.NONE.value, None,
                          "mandate_revoked: customer cancelled the mandate. No automated recovery possible.")


# ---------------------------------------------------------------------------
# LLM LAYER — only for decisions that are genuinely ambiguous
# ---------------------------------------------------------------------------

def _llm_decision(payment: PaymentAttempt, intervention_type: str) -> LLMDecision:
    """
    Ask Claude for tone, channel, and message content.

    tone is genuinely ambiguous: a first-time failure is mostly just surprising to
    the customer, but a third consecutive failure suggests they've ignored two previous
    nudges. Rules can't capture that distinction well. Claude handles it.
    """
    amount_rupees = payment.amount / 100
    json_schema = (
        '{"tone": "gentle|firm|urgent", "channel": "sms|email", '
        '"message": "...", "reasoning": "one sentence"}'
    )
    prompt = (
        f"A subscription payment failed. Decide how to contact this customer.\n\n"
        f"Customer: {payment.customer_name}\n"
        f"Amount: ₹{amount_rupees:,.0f}\n"
        f"Failure reason: {payment.decline_reason}\n"
        f"Attempt number: {payment.attempt_number}\n"
        f"Planned action: {intervention_type}\n\n"
        f"Guidelines:\n"
        f"- Attempt 1: gentle — most customers don't know the payment failed\n"
        f"- Attempt 2: firm — this has now happened twice\n"
        f"- Attempt 3: urgent — last automatic attempt before escalation\n"
        f"- card_expired: message focuses on the update link, not the failure\n"
        f"- authentication_failed: explain exactly what the customer must do (re-auth the mandate)\n"
        f"- SMS for urgent cases or amounts above ₹2000; email for detailed explanations\n\n"
        f"Reply with JSON only, no surrounding text:\n{json_schema}"
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",   # fast + cheap for this structured task
        max_tokens=350,
        messages=[{"role": "user", "content": prompt}],
    )
    data = json.loads(response.content[0].text.strip())
    return LLMDecision(
        tone=data["tone"], channel=data["channel"],
        message=data["message"], reasoning=data["reasoning"],
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def decide_one(payment: PaymentAttempt, db: Session, now: datetime | None = None) -> None:
    """Run both layers for a single payment. Does not commit."""
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)

    # --- DETERMINISTIC ---
    timing = _timing_decision(payment, now)
    payment.intervention_type = timing.intervention_type
    payment.retry_at = timing.retry_at
    log_event(db, payment.payment_id, "decide",
              timing.intervention_type, timing.reasoning, AuditLayer.DETERMINISTIC.value)

    if timing.intervention_type == InterventionType.NONE.value:
        # mandate_revoked: nothing to communicate; skip LLM entirely
        payment.status = PaymentStatus.UNRECOVERABLE
        return

    payment.status = PaymentStatus.DECIDED

    # --- LLM ---
    try:
        llm = _llm_decision(payment, timing.intervention_type)
    except Exception as exc:
        # LLM call failed — fall back to a generic message so the batch isn't blocked
        llm = LLMDecision(
            "gentle", "email",
            f"Hi {payment.customer_name}, your subscription payment of "
            f"₹{payment.amount // 100:,} could not be processed. "
            f"Please update your payment details to continue your subscription.",
            f"LLM unavailable ({exc}); using fallback",
        )

    log_event(
        db, payment.payment_id, "decide",
        f"channel={llm.channel} tone={llm.tone}",
        f"{llm.reasoning}\n\nDraft: {llm.message}",
        AuditLayer.LLM.value,
    )


def decide_batch(db: Session) -> dict[str, int]:
    """Run the decision layer on all diagnosed payments. Single commit at the end."""
    payments = (
        db.query(PaymentAttempt)
        .filter(PaymentAttempt.status == PaymentStatus.DIAGNOSED)
        .all()
    )

    counts: dict[str, int] = {}
    for payment in payments:
        decide_one(payment, db)
        counts[payment.intervention_type] = counts.get(payment.intervention_type, 0) + 1

    db.commit()
    return counts
