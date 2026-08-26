"""
Runs the generator and loads the output into the database.
Safe to re-run — clears existing agent-generated data before inserting fresh records.
"""

from collections import defaultdict
from datetime import datetime

from db.models import AuditEvent, ComplianceState, PaymentAttempt
from db.session import get_db, init_db

from data.generator import generate_batch


def clear_existing_data(db) -> None:
    """Delete all agent-generated rows before re-seeding."""
    # audit_events has a FK into payment_attempts — must go first or SQLite will
    # raise an integrity error even though we're deleting both
    db.query(AuditEvent).delete()
    db.query(PaymentAttempt).delete()
    db.query(ComplianceState).delete()
    db.commit()


def build_compliance_state(payments: list[dict]) -> list[dict]:
    """Aggregate payment records by customer to build one ComplianceState row each."""
    by_customer: dict[str, list[dict]] = defaultdict(list)
    for p in payments:
        by_customer[p["customer_id"]].append(p)

    states = []
    for cid, cust_payments in by_customer.items():
        opted_out    = any(p["opted_out"] for p in cust_payments)
        last_attempt = max(p["created_at"] for p in cust_payments)
        states.append({
            "customer_id":      cid,
            "attempt_count":    len(cust_payments),
            "last_attempted_at": last_attempt,
            "cooldown_until":   None,   # diagnoser sets this per-reason in Phase 3
            "opted_out":        opted_out,
            "opted_out_at":     last_attempt if opted_out else None,
        })
    return states


def _print_summary(payments: list[dict]) -> None:
    """Print distribution tables so the Phase 1c verification is readable at a glance."""
    total = len(payments)
    print(f"\nSeeded {total} payment records.\n")

    # failure_code — what the bank returned (bank_declined will be inflated)
    fc_counts: dict[str, int] = defaultdict(int)
    dr_counts: dict[str, int] = defaultdict(int)
    opt_out = 0
    for p in payments:
        fc_counts[p["failure_code"]]  += 1
        dr_counts[p["decline_reason"]] += 1
        if p["opted_out"]:
            opt_out += 1

    print("failure_code distribution (raw bank response):")
    for code, cnt in sorted(fc_counts.items(), key=lambda x: -x[1]):
        print(f"  {code:<25} {cnt:>4}  ({cnt/total*100:.1f}%)")

    print("\ndecline_reason distribution (ground truth):")
    for reason, cnt in sorted(dr_counts.items(), key=lambda x: -x[1]):
        print(f"  {reason:<25} {cnt:>4}  ({cnt/total*100:.1f}%)")

    attempt_counts: dict[int, int] = defaultdict(int)
    for p in payments:
        attempt_counts[p["attempt_number"]] += 1
    print("\nattempt_number distribution:")
    for n in sorted(attempt_counts):
        print(f"  attempt {n}: {attempt_counts[n]}")

    diverged = sum(1 for p in payments if p["failure_code"] != p["decline_reason"])
    print(f"\nDivergent records (failure_code != decline_reason): {diverged}")
    print(f"Opted-out customers: {opt_out}")
    print(f"Unique customers: {len(set(p['customer_id'] for p in payments))}\n")


def seed(db) -> None:
    """Generate a fresh batch and load it into the database."""
    payments = generate_batch()

    clear_existing_data(db)

    for p in payments:
        db.add(PaymentAttempt(
            payment_id      = p["payment_id"],
            customer_id     = p["customer_id"],
            customer_name   = p["customer_name"],
            customer_email  = p["customer_email"],
            customer_phone  = p["customer_phone"],
            subscription_id = p["subscription_id"],
            amount          = p["amount"],
            currency        = p["currency"],
            failure_code    = p["failure_code"],
            decline_reason  = p["decline_reason"],
            raw_metadata    = p["raw_metadata"],
            attempt_number  = p["attempt_number"],
            status          = p["status"],
            opted_out       = p["opted_out"],
            intervention_type = p["intervention_type"],
            retry_at          = p["retry_at"],
            created_at        = p["created_at"],
        ))

    db.commit()

    states = build_compliance_state(payments)
    for s in states:
        db.add(ComplianceState(
            customer_id      = s["customer_id"],
            attempt_count    = s["attempt_count"],
            last_attempted_at= s["last_attempted_at"],
            cooldown_until   = s["cooldown_until"],
            opted_out        = s["opted_out"],
            opted_out_at     = s["opted_out_at"],
        ))

    db.commit()
    _print_summary(payments)


if __name__ == "__main__":
    init_db()
    with get_db() as db:
        seed(db)
