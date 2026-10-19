"""
SIMULATED card issuers: named after the card's issuer account (Chase, Citi,
American Express …), or a generic simulated issuer when none is on file.
Approves or declines from the card fixture: account status, available credit,
the demo "decline" card, and demo scenario overrides. Not a real issuer.
"""
import secrets
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.schema import CardAccount, PaymentMethod
from backend.vic.common import now


def authorize(db: Session, card: PaymentMethod, acct: Optional[CardAccount], amount: float,
              scenario: Optional[str]) -> dict:
    name = f"Simulated {acct.issuer} issuer" if acct and acct.issuer else "Simulated card issuer"
    t = now()
    if (card.exp_year, card.exp_month) < (t.year, t.month):
        return {"actor": name, "result": "declined", "reason": "CARD_EXPIRED", "detail": "Card expired"}
    if acct and acct.status != "active":
        return {"actor": name, "result": "declined", "reason": "CARD_DECLINED", "detail": f"Account {acct.status}"}
    if scenario == "decline" or card.behaviour == "decline":
        return {"actor": name, "result": "declined", "reason": "CARD_DECLINED",
                "detail": "Issuer declined (demo decline " + ("scenario)" if scenario == "decline" else "card)")}
    if acct and acct.available_credit is not None and acct.available_credit < amount:
        return {"actor": name, "result": "declined", "reason": "INSUFFICIENT_CREDIT", "detail": "Over available credit"}
    if acct and acct.available_credit is not None:
        acct.available_credit = round(acct.available_credit - amount, 2)     # authorization hold
    return {"actor": name, "result": "approved", "reason": None,
            "auth_code": "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6)),
            "detail": f"Approved; ${amount:.2f} held against available credit"}


def release(db: Session, card: Optional[PaymentMethod], amount: float) -> None:
    """Void: give the held amount back."""
    if not card:
        return
    acct = db.query(CardAccount).filter_by(payment_method_id=card.payment_method_id).first()
    if acct and acct.available_credit is not None:
        acct.available_credit = round(acct.available_credit + amount, 2)
