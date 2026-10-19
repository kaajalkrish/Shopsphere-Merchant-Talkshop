"""
AgentTokenEnrollmentService — simulated agent-specific payment tokens.

  NOT_ENROLLED → (start) PENDING_AUTHENTICATION → (simulated passkey) ACTIVE
  ACTIVE ⇄ SUSPENDED          ACTIVE / SUSPENDED / PENDING → REVOKED (final)

A token binds one member + one permitted agent (Talkshop AI) + one Chase Visa
credit-card fixture. The reference (vat_…) is random; it is not derived from
card digits and is not a Visa Token Service / network token. No card number
or CVV is ever collected.
"""
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.schema import AgentToken, CardAccount
from backend.db.session_utils import write_audit_event
from backend.vic import passkey
from backend.vic.common import TALKSHOP_AGENT_ID, VicError, new_ref, now, short

LIVE = ("PENDING_AUTHENTICATION", "ACTIVE", "SUSPENDED")


def _visa_account(db: Session, user_id: str, card_account_id: str) -> CardAccount:
    acct = db.query(CardAccount).filter_by(account_id=card_account_id, user_id=user_id).first()
    if not acct:
        raise VicError("UNKNOWN_CARD_ACCOUNT", "That card isn't on your account.", 404)
    # CH-9: a Visa-network capability (flag set by the simulated issuer), not "is a Chase card"
    if acct.account_type != "credit_card" or acct.network != "Visa" or acct.status != "active" \
            or not acct.agent_pay_capable or not acct.payment_method_id:
        raise VicError("NOT_VISA_ELIGIBLE", "Agent payments need an active Visa credit card that supports them.", 400)
    return acct


def current(db: Session, user_id: str, card_account_id: str) -> Optional[AgentToken]:
    """The live (not revoked) token for this card, if any."""
    return (db.query(AgentToken)
            .filter(AgentToken.user_id == user_id, AgentToken.card_account_id == card_account_id,
                    AgentToken.agent_id == TALKSHOP_AGENT_ID, AgentToken.status.in_(LIVE))
            .order_by(AgentToken.id.desc()).first())


def status(db: Session, user_id: str, card_account_id: str) -> str:
    tok = current(db, user_id, card_account_id)
    if tok:
        return tok.status
    revoked = db.query(AgentToken).filter_by(user_id=user_id, card_account_id=card_account_id,
                                             status="REVOKED").first()
    return "REVOKED" if revoked else "NOT_ENROLLED"


def active_for_payment_method(db: Session, user_id: str, payment_method_id: Optional[str]) -> Optional[AgentToken]:
    if not payment_method_id:
        return None
    return db.query(AgentToken).filter_by(user_id=user_id, payment_method_id=payment_method_id,
                                          agent_id=TALKSHOP_AGENT_ID, status="ACTIVE").first()


def view(db: Session, user_id: str, acct: CardAccount) -> dict:
    tok = current(db, user_id, acct.account_id)
    return {"card_account_id": acct.account_id, "network": acct.network, "last4": acct.last4,
            "status": status(db, user_id, acct.account_id),
            "agent": "Talkshop AI", "agent_token": short(tok.agent_token_ref) if tok else None,
            "activated_at": tok.activated_at.isoformat() + "Z" if tok and tok.activated_at else None,
            "label": "Simulated agent token (not a Visa Token Service token)"}


def start(db: Session, user_id: str, card_account_id: str) -> tuple[AgentToken, dict]:
    acct = _visa_account(db, user_id, card_account_id)
    tok = current(db, user_id, acct.account_id)
    if tok and tok.status in ("ACTIVE", "SUSPENDED"):
        raise VicError("ALREADY_ENROLLED", f"Agent payments are already {tok.status.lower()} for this card.", 409)
    if not tok:
        tok = AgentToken(agent_token_ref=new_ref("vat"), user_id=user_id, card_account_id=acct.account_id,
                         payment_method_id=acct.payment_method_id, agent_id=TALKSHOP_AGENT_ID,
                         status="PENDING_AUTHENTICATION")
        db.add(tok)
        write_audit_event(db, "AGENT_TOKEN_REQUESTED", user_id=user_id, agent_id=TALKSHOP_AGENT_ID,
                          metadata={"agent_token": short(tok.agent_token_ref), "card_last4": acct.last4})
    ch, nonce = passkey.create(db, user_id, "enrollment", tok.agent_token_ref)
    db.commit()
    return tok, passkey.public(ch, nonce)


def complete(db: Session, user_id: str, card_account_id: str, challenge_id: str, nonce: str,
             user_verified: bool) -> AgentToken:
    acct = _visa_account(db, user_id, card_account_id)
    tok = current(db, user_id, acct.account_id)
    if not tok or tok.status != "PENDING_AUTHENTICATION":
        raise VicError("NOT_PENDING", "Start agent-payment setup first.", 409)
    try:
        passkey.verify(db, user_id, challenge_id, nonce, user_verified, "enrollment", tok.agent_token_ref)
    except VicError:
        db.commit()                     # the challenge stays used; the token stays pending
        raise
    tok.status, tok.activated_at = "ACTIVE", now()
    tok.passkey_credential_id = new_ref("pkc")      # the simulated passkey registered for step-up
    write_audit_event(db, "AGENT_TOKEN_ACTIVATED", user_id=user_id, agent_id=TALKSHOP_AGENT_ID,
                      metadata={"agent_token": short(tok.agent_token_ref), "auth": passkey.METHOD})
    db.commit()
    return tok


def set_status(db: Session, user_id: str, card_account_id: str, action: str) -> AgentToken:
    """suspend | resume | revoke (member or demo controls)."""
    tok = current(db, user_id, card_account_id)
    if not tok:
        raise VicError("NOT_ENROLLED", "Agent payments aren't set up for this card.", 404)
    target = {"suspend": "SUSPENDED", "resume": "ACTIVE", "revoke": "REVOKED"}.get(action)
    allowed = {"suspend": ("ACTIVE",), "resume": ("SUSPENDED",), "revoke": LIVE}
    if not target or tok.status not in allowed[action]:
        raise VicError("INVALID_TOKEN_ACTION", f"Can't {action} a token that is {tok.status.lower()}.", 409)
    tok.status, tok.status_reason = target, f"member_{action}"
    write_audit_event(db, f"AGENT_TOKEN_{target}", user_id=user_id, agent_id=TALKSHOP_AGENT_ID,
                      metadata={"agent_token": short(tok.agent_token_ref)})
    if target != "ACTIVE":              # a token that can't pay cancels its open instructions
        from backend.vic import instructions
        instructions.cancel_for_token(db, tok.agent_token_ref, f"AGENT_TOKEN_{target}")
    db.commit()
    return tok


def seed_active(db: Session, user_id: str, card_account_id: str) -> AgentToken:
    """Demo seed: Persona A starts with an ACTIVE token (as if enrolled earlier)."""
    acct = _visa_account(db, user_id, card_account_id)
    tok = current(db, user_id, acct.account_id)
    if tok:
        return tok
    tok = AgentToken(agent_token_ref=new_ref("vat"), user_id=user_id, card_account_id=acct.account_id,
                     payment_method_id=acct.payment_method_id, agent_id=TALKSHOP_AGENT_ID, status="ACTIVE",
                     activated_at=now(), passkey_credential_id=new_ref("pkc"), status_reason="demo_seed")
    db.add(tok)
    db.commit()
    return tok
