"""
Linking Chase card benefits (Part 2; CH-2: benefits only, never access) — against
a SIMULATED Chase cardholder directory (issuer_cardholders). No real bank is
contacted. Every ShopSphere customer can shop without this; a match only unlocks
Chase-exclusive benefits.

A new customer gives the name and email on their Chase account plus the last
four digits of one card. All three must match the same active record:

  name    case-insensitive, extra spaces ignored
  email   case-insensitive
  last4   exactly

Safeguards:
  * one generic error, whichever field was wrong (no hints for guessing);
  * at most MAX_FAILURES failed attempts per email in LOCK_WINDOW;
  * only the last four digits are ever asked for — never a card number or CVV;
  * a record can be linked to one ShopSphere account only.

A real issuer would add a second factor (e.g. a one-time code to the email or
phone on file); the digits alone are not proof of ownership.

On a match, every active account of that cardholder (same name + email) is
linked: credit cards become card_accounts + masked saved cards; a checking
account is linked too but never grants Chase credit-card benefits.
"""
import os
import re
import secrets
from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.schema import CardAccount, CardVerificationAttempt, IssuerCardholder, PaymentMethod
from backend.vic.common import now

MAX_FAILURES = 5
LOCK_WINDOW = timedelta(minutes=15)

NO_MATCH = ("We couldn't verify those details with Chase (simulated). Use the name and email on your Chase "
            "account and the last four digits of your Chase credit card.")
LOCKED = "Too many attempts. For your security, please wait 15 minutes and try again."
NOT_CREDIT = ("Those details match a Chase checking account. Chase benefits on ShopSphere are for Chase "
              "credit-card holders.")


class VerificationError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _email(value: str) -> str:
    return (value or "").strip().casefold()


# Demo records (simulated). Personas A–E are already linked by init_db; these are
# free for new sign-ups. Your own record can be added in .env (DEMO_CARDHOLDERS).
DEMO_RECORDS = [
    ("CH_ALEX_VISA", "Alex Rivera", "alex.rivera@cardmember.demo", "credit_card", "Visa", "1188"),
    ("CH_JORDAN_MC", "Jordan Lee", "jordan.lee@cardmember.demo", "credit_card", "Mastercard", "2277"),
    ("CH_SAM_VISA", "Sam Patel", "sam.patel@cardmember.demo", "credit_card", "Visa", "3366"),
    ("CH_SAM_MC", "Sam Patel", "sam.patel@cardmember.demo", "credit_card", "Mastercard", "3367"),
    ("CH_TAYLOR_CHK", "Taylor Kim", "taylor.kim@cardmember.demo", "checking", None, "9900"),
]
PERSONA_RECORDS = [
    ("CH_A_VISA", "Kaajal", "kaajal@shopsphere.demo", "credit_card", "Visa", "4821", "USR001"),
    ("CH_A_MC", "Kaajal", "kaajal@shopsphere.demo", "credit_card", "Mastercard", "0019", "USR001"),
    ("CH_B_MC", "Marcus", "marcus@shopsphere.demo", "credit_card", "Mastercard", "5510", "USR002"),
    ("CH_C_CHK", "Priya", "priya@shopsphere.demo", "checking", None, "7702", "USR003"),
    ("CH_E_VISA", "Elena", "elena@shopsphere.demo", "credit_card", "Visa", "4002", "USR004"),
    ("CH_H_VISA", "Chris Park", "chris@shopsphere.demo", "credit_card", "Visa", "1234", "USR007"),
]


def _env_records() -> list[tuple]:
    """DEMO_CARDHOLDERS="Full Name|email|last4|Visa;Full Name|email|last4|Mastercard" (kept out of git in .env)."""
    out = []
    for i, item in enumerate(filter(None, (os.getenv("DEMO_CARDHOLDERS") or "").split(";"))):
        parts = [x.strip() for x in item.split("|")]
        if len(parts) != 4 or not re.fullmatch(r"\d{4}", parts[2]):
            continue
        name, email, last4, network = parts
        kind = "checking" if network.lower() == "checking" else "credit_card"
        out.append((f"CH_ENV_{last4}_{i}", name, email, kind, None if kind == "checking" else network.title(), last4))
    return out


def seed(db: Session) -> None:
    for rid, name, email, kind, network, last4, user_id in PERSONA_RECORDS:
        if not db.query(IssuerCardholder).filter_by(record_id=rid).first():
            db.add(IssuerCardholder(record_id=rid, full_name=name, email=email, account_type=kind, network=network,
                                    last4=last4, claimed_by=user_id))
    for rid, name, email, kind, network, last4 in DEMO_RECORDS + _env_records():
        if not db.query(IssuerCardholder).filter_by(record_id=rid).first():
            db.add(IssuerCardholder(record_id=rid, full_name=name, email=email, account_type=kind, network=network,
                                    last4=last4))
    db.commit()


def _check_lock(db: Session, key: str) -> None:
    since = now() - LOCK_WINDOW
    fails = (db.query(CardVerificationAttempt)
             .filter(CardVerificationAttempt.key == key, CardVerificationAttempt.succeeded.is_(False),
                     CardVerificationAttempt.created_at >= since).count())
    if fails >= MAX_FAILURES:
        raise VerificationError("TOO_MANY_ATTEMPTS", LOCKED, 429)


def _fail(db: Session, key: str, code: str = "NO_MATCH", message: str = NO_MATCH, status: int = 400):
    db.add(CardVerificationAttempt(key=key, succeeded=False))
    db.commit()
    raise VerificationError(code, message, status)


def match(db: Session, full_name: str, email: str, last4: str) -> list[IssuerCardholder]:
    """The cardholder's active, unlinked records if name + email + last4 match one of them.
    Raises VerificationError otherwise (and records the failed attempt)."""
    key = _email(email)[:220]
    _check_lock(db, key)
    if not re.fullmatch(r"\d{4}", (last4 or "").strip()):
        raise VerificationError("INVALID_LAST4", "Enter the last four digits of your card (numbers only).")
    records = [r for r in db.query(IssuerCardholder).filter(IssuerCardholder.status == "active")
               if _email(r.email) == key and _name(r.full_name) == _name(full_name)]
    hit = next((r for r in records if secrets.compare_digest(r.last4, last4.strip())), None)
    if not hit or hit.claimed_by:
        _fail(db, key)
    if hit.account_type != "credit_card":
        _fail(db, key, "NOT_ELIGIBLE", NOT_CREDIT, 403)
    return [r for r in records if not r.claimed_by]


def link(db: Session, user_id: str, records: list[IssuerCardholder], holder_name: str) -> list[CardAccount]:
    """Link the matched cardholder's accounts to this ShopSphere user. Caller commits."""
    linked = []
    has_default = db.query(PaymentMethod).filter_by(user_id=user_id, is_default=True).first() is not None
    for r in sorted(records, key=lambda r: (r.account_type != "credit_card", r.network != "Visa")):
        pm_id = None
        if r.account_type == "credit_card":
            pm_id = f"PM_{r.network[:4].upper()}_{r.last4}_{secrets.token_hex(3).upper()}"
            db.add(PaymentMethod(payment_method_id=pm_id, user_id=user_id, brand=r.network, last4=r.last4,
                                 issuer="CHASE", source="issuer_link", exp_month=9, exp_year=2030, cardholder_name=holder_name,
                                 token_ref=f"tok_{secrets.token_urlsafe(18)}", behaviour="approve",
                                 is_default=not has_default))
            has_default = True
        acct = CardAccount(account_id=f"ACCT_{secrets.token_hex(6).upper()}", user_id=user_id, issuer="Chase",
                           account_type=r.account_type,
                           product_name=f"Chase {r.network} credit card" if r.account_type == "credit_card"
                           else f"Chase {r.account_type} account",
                           network=r.network, last4=r.last4, status="active",
                           marketplace_eligible=r.account_type == "credit_card", payment_method_id=pm_id,
                           agent_pay_capable=r.account_type == "credit_card" and r.network == "Visa",
                           credit_limit=8000.0 if pm_id else None, available_credit=8000.0 if pm_id else None)
        db.add(acct)
        r.claimed_by = user_id
        linked.append(acct)
    db.add(CardVerificationAttempt(key=_email(records[0].email)[:220], succeeded=True))
    return linked


def view(acct: CardAccount) -> dict:
    return {"network": acct.network, "last4": acct.last4, "account_type": acct.account_type,
            "marketplace_eligible": bool(acct.marketplace_eligible)}
