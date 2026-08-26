"""
Tests for the decider. Two sections:

  Deterministic layer (_timing_decision): fully testable without mocking.
    card_expired must always produce payment_link. mandate_revoked must always
    produce none. Timing windows are exact enough to assert on.

  LLM layer (_llm_decision): mocked — we're testing that the right fields
    come back and are written to the audit trail, not what Claude actually says.
"""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agent.decider import LLMDecision, TimingDecision, _llm_decision, _timing_decision, decide_one
from db.models import (
    AuditEvent, Base, DeclineReason, InterventionType,
    PaymentAttempt, PaymentStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    """Fresh in-memory SQLite per test."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _payment(decline_reason: str, attempt: int = 1, amount: int = 99900) -> PaymentAttempt:
    """Minimal diagnosed PaymentAttempt for testing."""
    return PaymentAttempt(
        payment_id      = f"pay_test_{decline_reason[:10]}",
        customer_id     = "cust_test01",
        customer_name   = "Priya Sharma",
        customer_email  = "priya@example.com",
        subscription_id = "sub_test01",
        amount          = amount,
        currency        = "INR",
        failure_code    = decline_reason,
        decline_reason  = decline_reason,
        raw_metadata    = "{}",
        attempt_number  = attempt,
        status          = PaymentStatus.DIAGNOSED,
        opted_out       = False,
    )


# reference datetime: mid-month (outside salary window), deterministic for all tests
MID_MONTH = datetime(2026, 8, 15, 12, 0, 0)
IN_WINDOW  = datetime(2026, 8,  3, 12, 0, 0)   # day 3 — inside salary window


# ---------------------------------------------------------------------------
# _timing_decision — deterministic layer
# ---------------------------------------------------------------------------

def test_insufficient_funds_outside_salary_window():
    p = _payment(DeclineReason.INSUFFICIENT_FUNDS.value)
    result = _timing_decision(p, MID_MONTH)
    assert result.intervention_type == InterventionType.RETRY.value
    # should land on the 2nd of September (next month after August)
    assert result.retry_at.month == 9
    assert result.retry_at.day   == 2
    assert result.retry_at.year  == 2026


def test_insufficient_funds_inside_salary_window():
    p = _payment(DeclineReason.INSUFFICIENT_FUNDS.value)
    result = _timing_decision(p, IN_WINDOW)
    assert result.intervention_type == InterventionType.RETRY.value
    # 2 days after Aug 3 = Aug 5
    assert result.retry_at.day == 5
    assert result.retry_at.month == 8


def test_insufficient_funds_december_rollover():
    dec_mid = datetime(2026, 12, 15, 12, 0, 0)
    p = _payment(DeclineReason.INSUFFICIENT_FUNDS.value)
    result = _timing_decision(p, dec_mid)
    # should roll over to January 2027
    assert result.retry_at.month == 1
    assert result.retry_at.year  == 2027
    assert result.retry_at.day   == 2


def test_network_error_retries_in_30_minutes():
    p = _payment(DeclineReason.NETWORK_ERROR.value)
    result = _timing_decision(p, MID_MONTH)
    assert result.intervention_type == InterventionType.RETRY.value
    delta = result.retry_at - MID_MONTH
    assert delta.total_seconds() == 30 * 60


def test_card_expired_always_payment_link():
    p = _payment(DeclineReason.CARD_EXPIRED.value)
    result = _timing_decision(p, MID_MONTH)
    assert result.intervention_type == InterventionType.PAYMENT_LINK.value
    # retry_at must be None — there is no point scheduling a retry on an expired card
    assert result.retry_at is None


def test_bank_declined_36h_gap():
    p = _payment(DeclineReason.BANK_DECLINED.value)
    result = _timing_decision(p, MID_MONTH)
    assert result.intervention_type == InterventionType.RETRY.value
    delta = result.retry_at - MID_MONTH
    assert delta.total_seconds() == 36 * 3600


def test_authentication_failed_48h_gap():
    p = _payment(DeclineReason.AUTHENTICATION_FAILED.value)
    result = _timing_decision(p, MID_MONTH)
    assert result.intervention_type == InterventionType.RETRY.value
    delta = result.retry_at - MID_MONTH
    assert delta.total_seconds() == 48 * 3600


def test_risk_threshold_human_escalation():
    p = _payment(DeclineReason.RISK_THRESHOLD.value)
    result = _timing_decision(p, MID_MONTH)
    assert result.intervention_type == InterventionType.HUMAN_ESCALATION.value
    assert result.retry_at is None


def test_mandate_revoked_none_intervention():
    p = _payment(DeclineReason.MANDATE_REVOKED.value)
    result = _timing_decision(p, MID_MONTH)
    assert result.intervention_type == InterventionType.NONE.value
    assert result.retry_at is None


# ---------------------------------------------------------------------------
# _llm_decision — mocked (testing structure, not Claude's output)
# ---------------------------------------------------------------------------

def _mock_llm_response(tone="gentle", channel="email",
                       message="Please update your payment details.",
                       reasoning="First attempt; gentle tone appropriate."):
    """Build a mock Anthropic API response object."""
    mock_msg = MagicMock()
    mock_msg.content[0].text = json.dumps({
        "tone": tone, "channel": channel,
        "message": message, "reasoning": reasoning,
    })
    return mock_msg


def test_llm_decision_returns_correct_fields():
    p = _payment(DeclineReason.INSUFFICIENT_FUNDS.value, attempt=1)
    with patch("agent.decider.anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = _mock_llm_response(
            tone="gentle", channel="email"
        )
        result = _llm_decision(p, InterventionType.RETRY.value)
    assert result.tone    == "gentle"
    assert result.channel == "email"
    assert isinstance(result.message, str) and len(result.message) > 0
    assert isinstance(result.reasoning, str)


def test_llm_decision_passes_attempt_number_in_prompt():
    """The prompt must include attempt_number so Claude can calibrate tone."""
    p = _payment(DeclineReason.BANK_DECLINED.value, attempt=3)
    with patch("agent.decider.anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = _mock_llm_response()
        _llm_decision(p, InterventionType.RETRY.value)

    call_args = MockClient.return_value.messages.create.call_args
    prompt_text = call_args.kwargs["messages"][0]["content"]
    assert "3" in prompt_text   # attempt number must appear in prompt


def test_llm_decision_uses_haiku_model():
    p = _payment(DeclineReason.BANK_DECLINED.value)
    with patch("agent.decider.anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = _mock_llm_response()
        _llm_decision(p, InterventionType.RETRY.value)

    call_args = MockClient.return_value.messages.create.call_args
    # haiku is fast and cheap — right model for this structured task
    assert "haiku" in call_args.kwargs["model"]


# ---------------------------------------------------------------------------
# decide_one — integration (both layers, audit events)
# ---------------------------------------------------------------------------

def test_decide_one_mandate_revoked_skips_llm(db):
    """mandate_revoked: status must be unrecoverable, exactly one audit event, no LLM call."""
    p = _payment(DeclineReason.MANDATE_REVOKED.value)
    db.add(p); db.commit()

    with patch("agent.decider.anthropic.Anthropic") as MockClient:
        decide_one(p, db, now=MID_MONTH)
        db.commit()
        MockClient.assert_not_called()

    assert p.status == PaymentStatus.UNRECOVERABLE
    assert p.intervention_type == InterventionType.NONE.value
    events = db.query(AuditEvent).filter_by(payment_id=p.payment_id).all()
    assert len(events) == 1
    assert events[0].layer == "deterministic"


def test_decide_one_writes_two_audit_events(db):
    """Normal path: one deterministic event for timing, one LLM event for message."""
    p = _payment(DeclineReason.INSUFFICIENT_FUNDS.value)
    db.add(p); db.commit()

    with patch("agent.decider.anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = _mock_llm_response()
        decide_one(p, db, now=MID_MONTH)
        db.commit()

    assert p.status == PaymentStatus.DECIDED
    events = db.query(AuditEvent).filter_by(payment_id=p.payment_id).all()
    layers = {e.layer for e in events}
    assert len(events) == 2
    assert "deterministic" in layers
    assert "llm" in layers


def test_decide_one_falls_back_on_llm_error(db):
    """If the LLM call throws, the fallback message should be logged and status stays decided."""
    p = _payment(DeclineReason.BANK_DECLINED.value)
    db.add(p); db.commit()

    with patch("agent.decider.anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.side_effect = Exception("API error")
        decide_one(p, db, now=MID_MONTH)
        db.commit()

    assert p.status == PaymentStatus.DECIDED
    llm_event = db.query(AuditEvent).filter_by(
        payment_id=p.payment_id, layer="llm"
    ).first()
    assert llm_event is not None
    assert "fallback" in llm_event.reasoning.lower()


def test_decide_one_card_expired_gets_payment_link(db):
    p = _payment(DeclineReason.CARD_EXPIRED.value)
    db.add(p); db.commit()

    with patch("agent.decider.anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = _mock_llm_response()
        decide_one(p, db, now=MID_MONTH)
        db.commit()

    assert p.intervention_type == InterventionType.PAYMENT_LINK.value
    assert p.retry_at is None


def test_decide_one_sets_retry_at(db):
    p = _payment(DeclineReason.NETWORK_ERROR.value)
    db.add(p); db.commit()

    with patch("agent.decider.anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = _mock_llm_response()
        decide_one(p, db, now=MID_MONTH)
        db.commit()

    assert p.retry_at is not None
    delta = p.retry_at - MID_MONTH
    assert delta.total_seconds() == 30 * 60
