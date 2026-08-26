"""
Generates a realistic batch of ~275 failed subscription payment records.

Real payment failure data is skewed and messy. This generator captures three
things most synthetic datasets miss: the failure_code vs. decline_reason split
(banks often return 'bank_declined' when the card is actually expired), temporal
clustering around billing cycles and bank maintenance windows, and a long tail
of genuinely unrecoverable cases so the final report's 'unresolved' section
has real content.
"""

import json
import random
import uuid
from datetime import datetime, timedelta

from faker import Faker

RANDOM_SEED = 42
TARGET_TOTAL = 275
CHRONIC_COUNT = 15   # customers permanently stuck at attempt 3, insufficient_funds
OPT_OUT_COUNT = 5    # customers who have explicitly opted out

DECLINE_DISTRIBUTION = [
    ("insufficient_funds",   0.35),
    ("bank_declined",        0.25),
    ("card_expired",         0.15),
    ("authentication_failed",0.10),
    ("risk_threshold",       0.08),
    ("network_error",        0.04),
    ("mandate_revoked",      0.03),
]

PLAN_TIERS = [
    ("Basic",      49900, 0.35),
    ("Standard",   99900, 0.30),
    ("Pro",       149900, 0.20),
    ("Business",  249900, 0.12),
    ("Enterprise",499900, 0.03),
]

BANKS = [
    ("SBI",0.22),("HDFC",0.18),("ICICI",0.14),("Axis",0.10),
    ("Kotak",0.08),("PNB",0.07),("BOB",0.06),("Canara",0.05),
    ("Union",0.05),("Yes",0.03),("IDFC",0.02),
]

BILLING_DAYS = [1, 5, 10, 15, 25]
BATCH_START  = datetime(2026, 7, 12)
BATCH_END    = datetime(2026, 8, 25)

OPT_OUT_REASONS = [
    "card_expired", "bank_declined", "authentication_failed",
    "insufficient_funds", "risk_threshold",
]


def _pid() -> str:
    """Generate a Razorpay-style payment ID."""
    return "pay_" + uuid.uuid4().hex[:16]


def _sid() -> str:
    """Generate a Razorpay-style subscription ID."""
    return "sub_" + uuid.uuid4().hex[:12]


def _cid() -> str:
    """Generate a Razorpay-style customer ID."""
    return "cust_" + uuid.uuid4().hex[:12]


def _weighted_choice(rng: random.Random, options: list) -> str:
    """Pick from a list of (value, weight) pairs."""
    values, weights = zip(*options)
    return rng.choices(values, weights=weights, k=1)[0]


def _make_customer_pool(n: int, fake: Faker, rng: random.Random) -> list[dict]:
    """Generate n unique customers with stable Indian identity data."""
    pool = []
    domains = ["infosys.com", "tcs.com", "wipro.com", "hcltech.com", "techm.com"]
    for _ in range(n):
        first = fake.first_name()
        last  = fake.last_name()
        style = rng.randint(0, 2)
        if style == 0:
            email = f"{first.lower()}.{last.lower()}@gmail.com"
        elif style == 1:
            email = f"{first.lower()}{rng.randint(10, 99)}@outlook.com"
        else:
            email = f"{first[0].lower()}{last.lower()}@{rng.choice(domains)}"
        # mobile numbers must start with 6-9; 0-5 are landlines or unassigned
        start  = rng.choice([6, 7, 8, 9])
        rest   = rng.randint(100_000_000, 999_999_999)
        phone  = f"+91{start}{rest}"
        pool.append({
            "customer_id":    _cid(),
            "customer_name":  f"{first} {last}",
            "customer_email": email,
            "customer_phone": phone,
            "subscription_id": _sid(),
        })
    return pool


def _compute_amount(base_paise: int, rng: random.Random) -> int:
    """Return a paise amount with realistic noise — coupons, GST, pro-ration."""
    r = rng.random()
    if r < 0.30:
        # coupon: round % discount but messy resulting amount
        discount = rng.uniform(0.05, 0.15)
        return int(base_paise * (1 - discount))
    elif r < 0.55:
        # GST at 18% — common on B2B and annual plans
        return int(base_paise * 1.18)
    elif r < 0.70:
        # pro-rated mid-cycle: customer upgraded partway through the billing period
        days = rng.randint(8, 22)
        return int(base_paise / 30 * days)
    else:
        # near-clean but add a 1–2 rupee processing fee so it's never exactly round
        return base_paise + rng.choice([0, 100, 200])


def _pick_billing_date(true_reason: str, rng: random.Random) -> datetime:
    """Return a UTC-naive created_at timestamp reflecting real mandate processing patterns."""
    # pick a billing date within the batch window
    day_of_month = rng.choice(BILLING_DAYS)
    month = rng.choice([7, 8])
    year  = 2026

    # card_expired: charge fails near month-end, when cards run out on EOM
    if true_reason == "card_expired":
        day_of_month = rng.randint(26, 28)

    # insufficient_funds: salary credits on 1st-5th, so accounts are temporarily healthy
    # then; failures cluster on 20th-28th when balances dip again
    if true_reason == "insufficient_funds" and day_of_month <= 5:
        day_of_month = rng.randint(20, 28)

    # clamp to valid days in the month (July has 31, August capped at 25)
    if month == 8:
        day_of_month = min(day_of_month, 25)

    base = datetime(year, month, day_of_month)

    # time-of-day: network_error hits during bank maintenance (2-4 AM IST = 20:30-22:30 UTC)
    # everything else processes during NACH batch windows (10 AM-2 PM IST = 4:30-8:30 UTC)
    if true_reason == "network_error":
        hour_utc   = rng.randint(20, 21)
        minute_utc = rng.randint(30, 59)
        base = base - timedelta(days=1)
    else:
        hour_utc   = rng.randint(4, 8)
        minute_utc = rng.randint(0, 59)

    return base.replace(hour=hour_utc, minute=minute_utc, second=rng.randint(0, 59))


def _resolve_failure_code(true_reason: str, rng: random.Random) -> str:
    """Map true reason to the raw failure_code the bank actually returned."""
    # banks frequently hide card_expired behind a generic 'bank_declined' —
    # the diagnoser catches this by checking the card expiry in raw_metadata
    if true_reason == "card_expired" and rng.random() < 0.60:
        return "bank_declined"
    # some mandate auth failures also come back as generic bank declines
    if true_reason == "authentication_failed" and rng.random() < 0.30:
        return "bank_declined"
    return true_reason


def _build_raw_metadata(
    true_reason: str, failure_code: str, bank: str,
    method: str, rng: random.Random
) -> str:
    """Build a JSON string mimicking a Razorpay bank webhook payload."""
    networks = [("Visa", 0.45), ("Mastercard", 0.40), ("RuPay", 0.15)]
    card_network = _weighted_choice(rng, networks)
    last4 = str(rng.randint(1000, 9999))

    base = {"bank_name": bank, "payment_method": method}

    if true_reason == "insufficient_funds":
        base.update({
            "error_code": "INSUFFICIENT_BALANCE",
            "error_description": "Insufficient funds in account",
            "error_source": "bank",
            "account_type": rng.choice(["savings", "current"]),
            "acquirer_data": {"bank_transaction_id": f"TXN{rng.randint(10**11, 10**12-1)}"},
        })
    elif true_reason == "bank_declined":
        base.update({
            "error_code": "DO_NOT_HONOR",
            "error_description": "Transaction declined by issuing bank",
            "error_source": "bank",
            "acquirer_data": {"rrn": str(rng.randint(10**11, 10**12-1))},
        })
    elif true_reason == "card_expired":
        # expired card: month 1-7 of 2026, or any month of 2025 — never Aug 2026+
        exp_year  = rng.choice([2025, 2025, 2026])
        exp_month = rng.randint(1, 7) if exp_year == 2026 else rng.randint(1, 12)
        base.update({
            "error_code": "CARD_EXPIRED" if failure_code == "card_expired" else "DO_NOT_HONOR",
            "error_description": "Transaction declined by issuing bank",
            "error_source": "bank",
            "card": {
                "network": card_network, "last4": last4,
                "expiry_month": exp_month, "expiry_year": exp_year,
                "issuer": bank,
            },
            "acquirer_data": {"rrn": str(rng.randint(10**11, 10**12-1))},
        })
    elif true_reason == "authentication_failed":
        auth_err = rng.choice(["AUTH_TIMEOUT", "OTP_INCORRECT", "AUTH_CANCELLED"])
        base.update({
            "error_code": auth_err if failure_code == "authentication_failed" else auth_err,
            "error_description": "Authentication failed — customer did not complete OTP",
            "error_source": "customer",
            "auth_type": "otp",
            "card": {"network": card_network, "last4": last4,
                     "expiry_month": rng.randint(1, 12), "expiry_year": rng.randint(2027, 2029)},
            "acquirer_data": {"rrn": str(rng.randint(10**11, 10**12-1))},
        })
    elif true_reason == "risk_threshold":
        flags = rng.sample(["unusual_amount","new_mandate","velocity_breach",
                            "device_mismatch","geo_anomaly"], k=rng.randint(1, 3))
        base.update({
            "error_code": "RISK_DECLINED",
            "error_description": "Payment blocked by risk engine",
            "error_source": "internal",
            "risk_score": rng.randint(65, 95),
            "risk_flags": flags,
            "review_required": True,
        })
    elif true_reason == "network_error":
        base.update({
            # error_source must be "gateway" — diagnoser uses this to distinguish
            # transient infrastructure failures from bank-side declines
            "error_code": "GATEWAY_TIMEOUT",
            "error_description": "Gateway connection timed out during mandate debit",
            "error_source": "gateway",
            "gateway_name": "Razorpay",
            "retry_eligible": True,
        })
    elif true_reason == "mandate_revoked":
        base.update({
            "error_code": "MANDATE_CANCELLED",
            "error_description": "Mandate revoked by customer",
            "error_source": "customer",
            "revocation_reason": "customer_initiated",
            "revoked_at": (BATCH_END - timedelta(days=rng.randint(1, 10))).isoformat(),
            "mandate_id": f"NACH{rng.randint(10**10, 10**11-1)}",
        })

    return json.dumps(base)


def _make_payment(customer: dict, true_reason: str, attempt_number: int,
                  created_at: datetime, opted_out: bool, rng: random.Random) -> dict:
    """Assemble one payment record dict from its parts."""
    tier_name  = _weighted_choice(rng, [(t[0], t[2]) for t in PLAN_TIERS])
    base_paise = next(t[1] for t in PLAN_TIERS if t[0] == tier_name)

    method = rng.choices(["emandate", "card"], weights=[0.80, 0.20])[0]
    bank   = _weighted_choice(rng, BANKS)
    fc     = _resolve_failure_code(true_reason, rng)

    return {
        "payment_id":      _pid(),
        "customer_id":     customer["customer_id"],
        "customer_name":   customer["customer_name"],
        "customer_email":  customer["customer_email"],
        "customer_phone":  customer["customer_phone"],
        "subscription_id": customer["subscription_id"],
        "amount":          _compute_amount(base_paise, rng),
        "currency":        "INR",
        "failure_code":    fc,
        "decline_reason":  true_reason,   # ground truth; diagnoser overwrites this in Phase 2
        "raw_metadata":    _build_raw_metadata(true_reason, fc, bank, method, rng),
        "attempt_number":  attempt_number,
        "status":          "pending",
        "opted_out":       opted_out,
        "intervention_type": None,
        "retry_at":          None,
        "created_at":        created_at,
    }


def generate_batch() -> list[dict]:
    """Return ~275 payment record dicts ready to be inserted into the DB."""
    rng = random.Random(RANDOM_SEED)
    Faker.seed(RANDOM_SEED)
    fake = Faker("en_IN")

    pool = _make_customer_pool(180, fake, rng)
    pool_idx = 0
    records: list[dict] = []

    # Pass 1 — chronic customers: always insufficient_funds, always at attempt 3.
    # These populate the 'unresolved' section of the final report — a 100% recovery
    # rate would be a red flag, so we bake in 15 cases that cannot be recovered.
    for _ in range(CHRONIC_COUNT):
        cust = pool[pool_idx]; pool_idx += 1
        t1 = _pick_billing_date("insufficient_funds", rng)
        t2 = t1 + timedelta(days=rng.randint(4, 6))
        t3 = t2 + timedelta(days=rng.randint(3, 5))
        for attempt, ts in enumerate([t1, t2, t3], start=1):
            records.append(_make_payment(cust, "insufficient_funds", attempt, ts, False, rng))

    # Pass 2 — opted-out customers: one payment each, opted_out=True.
    for i in range(OPT_OUT_COUNT):
        cust   = pool[pool_idx]; pool_idx += 1
        reason = OPT_OUT_REASONS[i]
        ts     = _pick_billing_date(reason, rng)
        records.append(_make_payment(cust, reason, 1, ts, True, rng))

    # Pass 3 — general pool: fills the remaining count with realistic distribution.
    reasons, weights = zip(*DECLINE_DISTRIBUTION)
    remaining = TARGET_TOTAL - len(records)
    attempt_weights = [0.65, 0.25, 0.10]   # 1st, 2nd, 3rd attempt

    added = 0
    while added < remaining:
        cust   = pool[pool_idx % len(pool)]; pool_idx += 1
        reason = rng.choices(reasons, weights=weights, k=1)[0]
        n_attempts = rng.choices([1, 2, 3], weights=attempt_weights, k=1)[0]
        n_attempts = min(n_attempts, remaining - added)

        t = _pick_billing_date(reason, rng)
        for attempt in range(1, n_attempts + 1):
            if attempt > 1:
                t = t + timedelta(days=rng.randint(3, 7))
            records.append(_make_payment(cust, reason, attempt, t, False, rng))
            added += 1
            if added >= remaining:
                break

    return records
