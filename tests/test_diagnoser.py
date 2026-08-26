"""
Tests for the diagnoser. Covers all seven decline reasons, both override paths
(bank_declined → card_expired, bank_declined → auth_failed), and the edge cases
around card expiry month boundaries.

_classify() is tested directly — it's pure logic, no DB needed.
diagnose_one() and diagnose_batch() use an in-memory SQLite DB so the audit
event write is exercised without touching the real data/recovery.db file.
"""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agent.diagnoser import _classify, _is_card_expired, diagnose_batch, diagnose_one
from db.models import Base, DeclineReason, PaymentAttempt, PaymentStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    """In-memory SQLite session — recreated fresh for each test."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _payment(failure_code: str, metadata: dict, attempt: int = 1, status: str = "pending") -> PaymentAttempt:
    """Build a minimal PaymentAttempt for testing without touching the DB."""
    return PaymentAttempt(
        payment_id      = f"pay_test_{failure_code[:8]}",
        customer_id     = "cust_test01",
        customer_name   = "Test User",
        customer_email  = "test@example.com",
        subscription_id = "sub_test01",
        amount          = 99900,
        currency        = "INR",
        failure_code    = failure_code,
        decline_reason  = None,
        raw_metadata    = json.dumps(metadata),
        attempt_number  = attempt,
        status          = status,
        opted_out       = False,
    )


# ---------------------------------------------------------------------------
# _is_card_expired — month boundary behaviour
# ---------------------------------------------------------------------------

def test_card_expired_clearly_in_past():
    assert _is_card_expired({"expiry_month": 3, "expiry_year": 2025}) is True


def test_card_expired_last_month():
    # July 2026 expired once August started
    assert _is_card_expired({"expiry_month": 7, "expiry_year": 2026}) is True


def test_card_not_expired_current_month():
    # cards expire at end of month — August 2026 is still valid on 2026-08-26
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc)
    assert _is_card_expired({"expiry_month": today.month, "expiry_year": today.year}) is False


def test_card_not_expired_future():
    assert _is_card_expired({"expiry_month": 6, "expiry_year": 2028}) is False


def test_card_missing_fields():
    # partial metadata shouldn't crash or false-positive
    assert _is_card_expired({}) is False
    assert _is_card_expired({"expiry_month": 3}) is False
    assert _is_card_expired({"expiry_year": 2025}) is False


# ---------------------------------------------------------------------------
# _classify — direct failure code matches (no override needed)
# ---------------------------------------------------------------------------

def test_classify_insufficient_funds():
    reason, text = _classify("insufficient_funds", {"bank_name": "HDFC"})
    assert reason == DeclineReason.INSUFFICIENT_FUNDS
    assert "insufficient" in text.lower()


def test_classify_network_error():
    reason, text = _classify("network_error", {"error_source": "gateway"})
    assert reason == DeclineReason.NETWORK_ERROR
    assert "gateway" in text.lower() or "transient" in text.lower()


def test_classify_risk_threshold():
    meta = {"risk_score": 82, "risk_flags": ["unusual_amount"]}
    reason, text = _classify("risk_threshold", meta)
    assert reason == DeclineReason.RISK_THRESHOLD
    assert "82" in text


def test_classify_mandate_revoked():
    reason, text = _classify("mandate_revoked", {"revocation_reason": "customer_initiated"})
    assert reason == DeclineReason.MANDATE_REVOKED
    assert "mandate" in text.lower() or "revok" in text.lower()


def test_classify_card_expired_direct():
    meta = {"card": {"expiry_month": 5, "expiry_year": 2025}}
    reason, _ = _classify("card_expired", meta)
    assert reason == DeclineReason.CARD_EXPIRED


def test_classify_authentication_failed_direct():
    meta = {"auth_type": "otp", "error_code": "AUTH_TIMEOUT"}
    reason, _ = _classify("authentication_failed", meta)
    assert reason == DeclineReason.AUTHENTICATION_FAILED


# ---------------------------------------------------------------------------
# _classify — bank_declined override paths (the core of the diagnoser)
# ---------------------------------------------------------------------------

def test_bank_declined_overrides_to_card_expired():
    # this is what 60% of card_expired cases look like in the raw data —
    # the bank returns DO_NOT_HONOR but the card's date is in the past
    meta = {
        "card": {"expiry_month": 10, "expiry_year": 2025, "last4": "1234"},
        "error_code": "DO_NOT_HONOR",
    }
    reason, text = _classify("bank_declined", meta)
    assert reason == DeclineReason.CARD_EXPIRED
    assert "override" in text.lower()
    assert "card_expired" in text.lower()


def test_bank_declined_overrides_to_auth_failed():
    # 30% of auth failures arrive as generic bank declines
    meta = {"auth_type": "otp", "error_code": "OTP_INCORRECT"}
    reason, text = _classify("bank_declined", meta)
    assert reason == DeclineReason.AUTHENTICATION_FAILED
    assert "override" in text.lower()
    assert "authentication_failed" in text.lower()


def test_bank_declined_stays_bank_declined_with_valid_card():
    # valid card in metadata should NOT trigger the card_expired override
    meta = {
        "card": {"expiry_month": 9, "expiry_year": 2028, "last4": "5678"},
        "error_code": "DO_NOT_HONOR",
    }
    reason, _ = _classify("bank_declined", meta)
    assert reason == DeclineReason.BANK_DECLINED


def test_bank_declined_stays_bank_declined_with_no_metadata():
    reason, _ = _classify("bank_declined", {})
    assert reason == DeclineReason.BANK_DECLINED


def test_card_expired_override_takes_priority_over_auth():
    # if somehow both an expired card AND auth_type appear, expired card wins
    # (expiry is checked first in the classification chain)
    meta = {
        "card": {"expiry_month": 1, "expiry_year": 2025},
        "auth_type": "otp",
    }
    reason, _ = _classify("bank_declined", meta)
    assert reason == DeclineReason.CARD_EXPIRED


def test_unknown_failure_code_defaults_to_bank_declined():
    reason, text = _classify("some_new_bank_code", {})
    assert reason == DeclineReason.BANK_DECLINED
    assert "unrecognised" in text.lower()


# ---------------------------------------------------------------------------
# diagnose_one — DB integration (audit event written, status updated)
# ---------------------------------------------------------------------------

def test_diagnose_one_updates_status_and_reason(db):
    payment = _payment("insufficient_funds", {"bank_name": "SBI"})
    db.add(payment)
    db.commit()

    diagnose_one(payment, db)
    db.commit()

    assert payment.status         == PaymentStatus.DIAGNOSED
    assert payment.decline_reason == DeclineReason.INSUFFICIENT_FUNDS.value


def test_diagnose_one_writes_audit_event(db):
    from db.models import AuditEvent
    payment = _payment("card_expired", {"card": {"expiry_month": 6, "expiry_year": 2025}})
    db.add(payment)
    db.commit()

    diagnose_one(payment, db)
    db.commit()

    events = db.query(AuditEvent).filter_by(payment_id=payment.payment_id).all()
    assert len(events) == 1
    assert events[0].step     == "diagnose"
    assert events[0].layer    == "deterministic"
    assert events[0].decision == DeclineReason.CARD_EXPIRED.value


def test_diagnose_one_override_card_expired(db):
    # bank_declined in the DB, but metadata shows expired card — diagnoser must override
    meta = {"card": {"expiry_month": 4, "expiry_year": 2026}, "error_code": "DO_NOT_HONOR"}
    payment = _payment("bank_declined", meta)
    db.add(payment)
    db.commit()

    diagnose_one(payment, db)
    db.commit()

    assert payment.decline_reason == DeclineReason.CARD_EXPIRED.value


# ---------------------------------------------------------------------------
# diagnose_batch — only processes pending payments
# ---------------------------------------------------------------------------

def test_diagnose_batch_skips_already_diagnosed(db):
    """Payments not in 'pending' status must not be re-diagnosed."""
    already_done = _payment("bank_declined", {}, status="diagnosed")
    already_done.decline_reason = "bank_declined"
    pending = _payment("network_error", {"error_source": "gateway"})
    pending.payment_id = "pay_testnet0000000"   # ensure unique payment_id

    db.add(already_done)
    db.add(pending)
    db.commit()

    counts = diagnose_batch(db)

    # only the pending one should appear in counts
    assert counts.get("network_error") == 1
    assert "bank_declined" not in counts


def test_diagnose_batch_returns_counts_by_reason(db):
    payments = [
        _payment("insufficient_funds", {"bank_name": "HDFC"}),
        _payment("mandate_revoked",    {"revocation_reason": "customer_initiated"}),
        _payment("risk_threshold",     {"risk_score": 75, "risk_flags": []}),
    ]
    # ensure unique payment_ids
    for i, p in enumerate(payments):
        p.payment_id = f"pay_batch_test{i:04d}"
    for p in payments:
        db.add(p)
    db.commit()

    counts = diagnose_batch(db)

    assert counts["insufficient_funds"] == 1
    assert counts["mandate_revoked"]    == 1
    assert counts["risk_threshold"]     == 1
