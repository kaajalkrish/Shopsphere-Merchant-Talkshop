"""
PaymentInstructionService — the member's authenticated instruction for ONE
exact purchase (Visa path).

  create        "Approve and pay" on an exact checkout → PENDING_AUTHENTICATION
                + a simulated passkey challenge
  authenticate  simulated passkey verified → AUTHENTICATED
  cancel / invalidate_for_checkout / cancel_for_token
                checkout terms changed, member cancelled, token suspended/revoked

An instruction carries: member, agent token, agent, merchant, checkout id +
hash, amount, currency, expiry and a permitted transaction count (1). The
assistant can't create an AUTHENTICATED instruction on its own: that needs
the member's passkey step.
"""
from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.schema import Checkout, PaymentInstruction
from backend.db.session_utils import write_audit_event
from backend.vic import enrollment, passkey
from backend.vic.common import (ALLOWED, MERCHANT_ID, TALKSHOP_AGENT_ID, VicError, log_event, new_ref, now, short,
                                transition)

INSTRUCTION_TTL = timedelta(minutes=10)
OPEN = ("PENDING_AUTHENTICATION", "AUTHENTICATED")


def get(db: Session, user_id: str, instruction_id: str) -> PaymentInstruction:
    instr = db.query(PaymentInstruction).filter_by(instruction_id=instruction_id, user_id=user_id).first()
    if not instr:
        raise VicError("UNKNOWN_INSTRUCTION", "Payment instruction not found.", 404)
    return instr


def view(instr: PaymentInstruction) -> dict:
    """Safe view: shortened refs, amounts, status. No secrets."""
    return {"instruction_id": instr.instruction_id, "status": instr.status,
            "lifecycle_state": instr.lifecycle_state, "checkout_id": instr.checkout_id,
            "merchant_id": instr.merchant_id, "amount": instr.amount, "currency": instr.currency,
            "max_transactions": instr.max_transactions, "transactions_used": instr.transactions_used,
            "agent_token": short(instr.agent_token_ref), "agent_id": instr.agent_id,
            "expires_at": instr.expires_at.isoformat() + "Z", "status_reason": instr.status_reason}


def _close(db: Session, instr: PaymentInstruction, status: str, reason: str, lifecycle: Optional[str]) -> None:
    instr.status, instr.status_reason = status, reason
    if lifecycle and lifecycle in ALLOWED.get(instr.lifecycle_state, set()):
        transition(db, instr, lifecycle, reason)
    else:
        log_event(db, instr.instruction_id, instr.user_id, lifecycle or status, reason)


def create(db: Session, user_id: str, checkout_id: str, consent_mode: str = "button") -> tuple[PaymentInstruction, dict]:
    from backend.shop import checkout as checkout_service
    from backend.shop import vic as vic_facade

    co = db.query(Checkout).filter_by(checkout_id=checkout_id, user_id=user_id).first()
    if not co:
        raise VicError("UNKNOWN_CHECKOUT", "Checkout not found.", 404)
    if co.status != "open":
        raise VicError("CHECKOUT_CLOSED", f"This checkout is {co.status}.", 409)
    issues = checkout_service.refresh(db, co)
    snap = checkout_service.snapshot(db, co, issues)
    if not snap["ready"]:
        db.commit()
        raise VicError("NOT_READY", "This order can't be placed yet.", 409, {"issues": snap["issues"]})
    path = vic_facade.payment_path(db, co)
    if path["path"] == "vic_setup_required":
        raise VicError("AGENT_PAY_NOT_ENROLLED",
                       "Set up agent payments for your Visa first (a quick simulated passkey step).", 409,
                       {"card_account_id": path.get("card_account_id")})
    if path["path"] != "vic":
        raise VicError("NOT_VIC_PATH", "This payment method doesn't use Visa agent payments.", 400)
    token = enrollment.active_for_payment_method(db, user_id, co.payment_method_id)

    # One open instruction per checkout: a new approval supersedes the old one.
    for old in db.query(PaymentInstruction).filter(PaymentInstruction.checkout_id == checkout_id,
                                                   PaymentInstruction.status.in_(OPEN)):
        _close(db, old, "CANCELLED", "SUPERSEDED", "INSTRUCTION_CANCELLED")

    instr = PaymentInstruction(
        instruction_id=new_ref("pin"), user_id=user_id, agent_token_ref=token.agent_token_ref,
        agent_id=TALKSHOP_AGENT_ID, merchant_id=MERCHANT_ID, checkout_id=co.checkout_id,
        checkout_hash=co.checkout_hash, checkout_version=co.version, amount=co.total, currency=co.currency,
        max_transactions=1,
        status="PENDING_AUTHENTICATION", consent_mode=consent_mode if consent_mode in ("button", "typed") else "button",
        lifecycle_state="CHECKOUT_CREATED", expires_at=now() + INSTRUCTION_TTL)
    db.add(instr)
    card = path["card"]
    log_event(db, instr.instruction_id, user_id, "CARD_LINKED", f"{card.get('display') or 'Visa card'} (fixture)")
    log_event(db, instr.instruction_id, user_id, "AGENT_TOKEN_ACTIVE", f"Agent token {short(token.agent_token_ref)}")
    log_event(db, instr.instruction_id, user_id, "CHECKOUT_CREATED",
              f"{co.checkout_id}: {co.currency} {co.total:.2f} at ShopSphere")
    write_audit_event(db, "PAYMENT_INSTRUCTION_CREATED", user_id=user_id, agent_id=TALKSHOP_AGENT_ID,
                      order_id=co.checkout_id, metadata={"instruction": short(instr.instruction_id),
                                                        "amount": co.total, "checkout_hash": co.checkout_hash,
                                                        "consent": instr.consent_mode})
    ch, nonce = passkey.create(db, user_id, "instruction", instr.instruction_id)
    db.commit()
    return instr, passkey.public(ch, nonce)


def authenticate(db: Session, user_id: str, instruction_id: str, challenge_id: str, nonce: str,
                 user_verified: bool) -> PaymentInstruction:
    instr = get(db, user_id, instruction_id)
    if instr.status != "PENDING_AUTHENTICATION":
        raise VicError("INSTRUCTION_NOT_PENDING", f"This instruction is {instr.status.lower()}.", 409)
    if instr.expires_at < now():
        _close(db, instr, "EXPIRED", "INSTRUCTION_EXPIRED", "INSTRUCTION_EXPIRED")
        db.commit()
        raise VicError("INSTRUCTION_EXPIRED", "This approval expired. Approve the order again.", 410)
    try:
        passkey.verify(db, user_id, challenge_id, nonce, user_verified, "instruction", instr.instruction_id)
    except VicError as exc:
        _close(db, instr, "FAILED", exc.code, "AUTHENTICATION_FAILED")
        db.commit()
        raise
    instr.status, instr.authenticated_at = "AUTHENTICATED", now()
    transition(db, instr, "INSTRUCTION_AUTHENTICATED", "Simulated passkey verified (single-use challenge)")
    write_audit_event(db, "PAYMENT_INSTRUCTION_AUTHENTICATED", user_id=user_id, agent_id=TALKSHOP_AGENT_ID,
                      order_id=instr.checkout_id, metadata={"instruction": short(instr.instruction_id),
                                                           "auth": passkey.METHOD})
    db.commit()
    return instr


def cancel(db: Session, user_id: str, instruction_id: str, reason: str = "MEMBER_CANCELLED") -> PaymentInstruction:
    instr = get(db, user_id, instruction_id)
    if instr.status in OPEN:
        _close(db, instr, "CANCELLED", reason, "INSTRUCTION_CANCELLED")
        db.commit()
    return instr


def invalidate_for_checkout(db: Session, checkout_id: str, current_hash: Optional[str],
                            reason: str = "CHECKOUT_CHANGED") -> int:
    """Checkout terms changed (or it was cancelled): open instructions for the
    old terms can never be used. Caller commits."""
    n = 0
    for instr in db.query(PaymentInstruction).filter(PaymentInstruction.checkout_id == checkout_id,
                                                     PaymentInstruction.status.in_(OPEN)):
        if current_hash is None or instr.checkout_hash != current_hash:
            _close(db, instr, "CANCELLED", reason, "INSTRUCTION_CANCELLED")
            n += 1
    return n


def cancel_for_token(db: Session, agent_token_ref: str, reason: str) -> None:
    for instr in db.query(PaymentInstruction).filter(PaymentInstruction.agent_token_ref == agent_token_ref,
                                                     PaymentInstruction.status.in_(OPEN)):
        _close(db, instr, "CANCELLED", reason, "INSTRUCTION_CANCELLED")
