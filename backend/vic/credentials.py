"""
VIC-inspired payment controls + PaymentCredentialService.

After GreenLight's DPAT and PayIt's 12 token checks pass, the simulator checks
the member's instruction against the live purchase (12 controls below) and only
then issues a purchase-specific credential reference (pcr_…): random, single
use, 5-minute life, bound to member + merchant + checkout + amount + currency.
The simulated cryptogram behind it is stored hashed and never leaves the
server-side services; the LLM, the chat and the trace only ever see a
shortened reference.
"""
import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from backend.db.schema import CardAccount, Checkout, Order, PaymentCredential, PaymentInstruction
from backend.shop import eligibility
from backend.vic import enrollment
from backend.vic.common import MERCHANT_ID, VicError, new_ref, now, sha256, short, transition

CREDENTIAL_TTL = timedelta(minutes=5)

CONTROL_NAMES = [
    "Customer session + access", "Active agent-pay capable Visa card (fixture)", "Active agent token",
    "Instruction authenticated", "Merchant matches", "Checkout id + hash match", "Amount + currency match",
    "Instruction not expired", "Transaction count + spending scope", "Not cancelled or consumed",
    "No duplicate payment attempt", "Inventory + final checkout state",
]


def run_controls(db: Session, user, instr: PaymentInstruction, co: Checkout, snapshot_ready: bool,
                 *, merchant_id: str = MERCHANT_ID) -> tuple[bool, list[dict]]:
    """Every control in order; stops at the first failure. Returns (passed, results)."""
    results: list[dict] = []

    def check(ok: bool, code: str, detail: str = "") -> bool:
        n = len(results) + 1
        results.append({"n": n, "name": CONTROL_NAMES[n - 1], "passed": bool(ok),
                        "reason": None if ok else code, "detail": detail})
        return bool(ok)

    acct = db.query(CardAccount).filter_by(user_id=user.user_id, payment_method_id=co.payment_method_id).first()
    token = enrollment.active_for_payment_method(db, user.user_id, co.payment_method_id)
    steps = [
        lambda: check(eligibility.check_access(db, user) is None and instr.user_id == user.user_id, "NOT_ELIGIBLE"),
        lambda: check(acct is not None and acct.network == "Visa" and acct.status == "active"
                      and acct.account_type == "credit_card" and bool(acct.agent_pay_capable), "VISA_CARD_INACTIVE"),
        lambda: check(token is not None and token.agent_token_ref == instr.agent_token_ref, "AGENT_TOKEN_INACTIVE"),
        lambda: check(instr.status == "AUTHENTICATED" and instr.authenticated_at is not None,
                      "INSTRUCTION_NOT_AUTHENTICATED"),
        lambda: check(instr.merchant_id == merchant_id == MERCHANT_ID, "MERCHANT_MISMATCH"),
        lambda: check(instr.checkout_id == co.checkout_id and secrets.compare_digest(
            instr.checkout_hash or "", co.checkout_hash or ""), "CHECKOUT_MISMATCH"),
        lambda: check(abs(instr.amount - co.total) < 0.005 and instr.currency == co.currency, "AMOUNT_MISMATCH",
                      f"{co.currency} {co.total:.2f}"),
        lambda: check(instr.expires_at > now(), "INSTRUCTION_EXPIRED"),
        lambda: check(instr.transactions_used < instr.max_transactions and co.total <= instr.amount + 0.005,
                      "TRANSACTION_LIMIT"),
        lambda: check(instr.status not in ("CANCELLED", "CONSUMED", "EXPIRED", "FAILED"), "INSTRUCTION_CLOSED"),
        lambda: check(not db.query(PaymentCredential).filter(PaymentCredential.instruction_id == instr.instruction_id)
                      .first() and not db.query(Order).filter(Order.checkout_id == co.checkout_id,
                                                              Order.status == "confirmed").first(),
                      "DUPLICATE_ATTEMPT"),
        lambda: check(snapshot_ready and co.status == "consented", "CHECKOUT_NOT_READY"),
    ]
    for step in steps:
        if not step():
            return False, results
    return True, results


def issue(db: Session, instr: PaymentInstruction) -> PaymentCredential:
    """Purchase-specific credential (after the controls pass). Caller commits."""
    cryptogram = secrets.token_urlsafe(32)          # simulated; only its hash is kept
    cred = PaymentCredential(credential_ref=new_ref("pcr"), instruction_id=instr.instruction_id,
                             user_id=instr.user_id, merchant_id=instr.merchant_id, checkout_id=instr.checkout_id,
                             amount=instr.amount, currency=instr.currency, cryptogram_hash=sha256(cryptogram),
                             status="ISSUED", expires_at=now() + CREDENTIAL_TTL)
    db.add(cred)
    instr.transactions_used += 1
    transition(db, instr, "CREDENTIAL_ISSUED", f"Credential {short(cred.credential_ref)}: single use, 5 min, "
                                               f"bound to ShopSphere + {instr.checkout_id} + {instr.currency} {instr.amount:.2f}")
    return cred


def redeem(db: Session, credential_ref: str, *, merchant_id: str, amount: float, currency: str,
           checkout_id: str, user_id: str) -> PaymentCredential:
    """The (simulated) network side: a credential is good once, only for what it was issued for.
    Raises VicError for unknown, expired, revoked, altered (wrong merchant/amount/checkout/member) or replayed."""
    from sqlalchemy import update
    cred = db.query(PaymentCredential).filter_by(credential_ref=credential_ref).first()
    if not cred:
        raise VicError("CREDENTIAL_UNKNOWN", "Unknown payment credential.", 404)
    if cred.status == "USED":
        raise VicError("CREDENTIAL_REPLAYED", "This payment credential was already used.", 409)
    if cred.status != "ISSUED":
        raise VicError("CREDENTIAL_" + cred.status, f"This payment credential is {cred.status.lower()}.", 409)
    if cred.expires_at < now():
        cred.status = "EXPIRED"
        raise VicError("CREDENTIAL_EXPIRED", "This payment credential expired.", 410)
    if cred.merchant_id != merchant_id:
        raise VicError("CREDENTIAL_MERCHANT_MISMATCH", "Credential not valid for this merchant.", 403)
    if cred.checkout_id != checkout_id or cred.user_id != user_id:
        raise VicError("CREDENTIAL_BINDING_MISMATCH", "Credential not valid for this purchase.", 403)
    if abs(cred.amount - amount) >= 0.005 or cred.currency != currency:
        raise VicError("CREDENTIAL_AMOUNT_MISMATCH", "Amount differs from what the member approved.", 403)
    instr = db.query(PaymentInstruction).filter_by(instruction_id=cred.instruction_id).first()
    if not instr or instr.status != "AUTHENTICATED":
        raise VicError("INSTRUCTION_NOT_ACTIVE", "The member's instruction is no longer active.", 409)
    claimed = db.execute(update(PaymentCredential)
                         .where(PaymentCredential.id == cred.id, PaymentCredential.status == "ISSUED")
                         .values(status="USED", used_at=now())).rowcount
    if not claimed:
        raise VicError("CREDENTIAL_REPLAYED", "This payment credential was already used.", 409)
    db.refresh(cred)
    return cred


def revoke(db: Session, credential_ref: str) -> None:
    cred = db.query(PaymentCredential).filter_by(credential_ref=credential_ref).first()
    if cred and cred.status == "ISSUED":
        cred.status = "REVOKED"
