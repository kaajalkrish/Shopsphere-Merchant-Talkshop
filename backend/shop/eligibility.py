"""
Marketplace access vs Chase benefits (CH-2). Deterministic, server-side, no AI.

Access is for every credit-card holder. Being a Chase cardholder is not an
access rule; it is an *entitlement* (Chase-exclusive offers, more points).

  browse    search, product pages, Talkshop, cart     everyone (visitors too)
  checkout  review an order, saved details, orders    a signed-in, active ShopSphere customer
                                                      (visitor → LOGIN_REQUIRED)
  pay       place the order                           a credit-card holder: an active credit card
                                                      on file (visitor/none → CREDIT_CARD_REQUIRED)

The issuer is never consulted for access. Chase helpers below
(chase_cards / has_chase_relationship) answer "which Chase benefits apply?".
All issuer data is a simulated fixture (card_accounts); nothing here talks to a bank.
"""
import secrets
from typing import Optional

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from backend.auth.dependencies import CurrentUser, get_current_user
from backend.db.schema import CardAccount, PaymentMethod, User
from backend.db.session_utils import get_session
from backend.shop import cards

ISSUER = "Chase"

MESSAGES = {
    "LOGIN_REQUIRED": "Sign in or create a ShopSphere account to check out. Your cart comes with you.",
    "ACCOUNT_INACTIVE": "This ShopSphere account isn't active.",
    "CREDIT_CARD_REQUIRED": "ShopSphere checkout is for credit-card holders, so the ShopSphere Wallet works once a credit "
                            "card is saved on your account, and there isn't one yet. Pay with a new card instead "
                            "(you can save it for next time).",
}


def check_access(db: Session, user: CurrentUser) -> Optional[str]:
    """Checkout-level access: None for a signed-in, active customer (any issuer, or
    no card yet), otherwise the denial code. The issuer is never part of this."""
    if user.is_visitor:
        return "LOGIN_REQUIRED"
    row = db.query(User).filter_by(user_id=user.user_id).first()
    if row is None or (row.status or "active") != "active":
        return "ACCOUNT_INACTIVE"
    return None


check = check_access   # older call sites (VIC control 1, Talkshop) ask the same question


def credit_cards(db: Session, user_id: str) -> list[PaymentMethod]:
    """The customer's active saved credit cards, any issuer and any network (demo:
    every saved card is a credit card)."""
    return (db.query(PaymentMethod)
            .filter(PaymentMethod.user_id == user_id, PaymentMethod.saved.isnot(False),
                    (PaymentMethod.status.is_(None)) | (PaymentMethod.status == "active"))
            .order_by(PaymentMethod.is_default.desc(), PaymentMethod.id.asc()).all())


def is_credit_card_holder(db: Session, user_id: str) -> bool:
    return bool(credit_cards(db, user_id))


def check_payment(db: Session, user: CurrentUser, pay_with: str = "card") -> Optional[str]:
    """Pay-level: access, and the customer is a credit-card holder. Paying by card,
    the selected card is the proof (a missing card is a checkout readiness issue,
    NO_PAYMENT_METHOD). Paying from the ShopSphere Wallet needs a credit card on file."""
    code = check_access(db, user)
    if code:
        return code
    if pay_with == "wallet" and not is_credit_card_holder(db, user.user_id):
        return "CREDIT_CARD_REQUIRED"
    return None


def chase_cards(db: Session, user_id: str) -> list[CardAccount]:
    """Active, eligible Chase credit-card accounts (the source of Chase benefits)."""
    return (db.query(CardAccount)
            .filter_by(user_id=user_id, issuer=ISSUER, account_type="credit_card",
                       status="active", marketplace_eligible=True)
            .order_by(CardAccount.id).all())


eligible_cards = chase_cards   # Part 2 name, kept for older callers


def has_chase_relationship(db: Session, user_id: str) -> bool:
    return bool(chase_cards(db, user_id))


def denial(code: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"code": code, "message": MESSAGES[code]})


async def require_shopper(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """FastAPI dependency for checkout-level APIs (checkout, saved details, orders,
    offers, rewards, agent payments). Hidden UI is not the control: this is."""
    with get_session() as db:
        code = check_access(db, user)
    if code:
        raise denial(code)
    return user


require_cardmember = require_shopper   # deprecated Part 2 name (CH-2); no longer means "Chase"


def card_view(acct: CardAccount) -> dict:
    """Masked metadata only — never a card number, CVV or token. "issuer" and
    "network" are display names (as before); CH-1 adds the canonical codes."""
    return {"account_id": acct.account_id, "issuer": acct.issuer, "account_type": acct.account_type,
            "product_name": acct.product_name, "network": acct.network, "last4": acct.last4,
            "status": acct.status, "marketplace_eligible": bool(acct.marketplace_eligible),
            "payment_method_id": acct.payment_method_id,
            "agent_pay_capable": bool(acct.agent_pay_capable) and acct.network == "Visa",
            "issuer_code": cards.normalize_issuer(acct.issuer),
            "network_code": cards.normalize_network(acct.network) if acct.network else None}


def accounts(db: Session, user_id: str) -> list[dict]:
    return [card_view(a) for a in db.query(CardAccount).filter_by(user_id=user_id).order_by(CardAccount.id)]


def card_account_for_payment_method(db: Session, user_id: str, payment_method_id: Optional[str]) -> Optional[CardAccount]:
    if not payment_method_id:
        return None
    return db.query(CardAccount).filter_by(user_id=user_id, payment_method_id=payment_method_id).first()


def link_demo_card(db: Session, user_id: str, *, network: str = "Visa", last4: Optional[str] = None,
                   account_type: str = "credit_card", eligible: bool = True, behaviour: str = "approve",
                   with_payment_method: bool = True, is_default: bool = True,
                   credit_limit: float = 8000.0, issuer: str = ISSUER,
                   agent_pay_capable: Optional[bool] = None) -> CardAccount:
    """Create a simulated issuer account for a user (seed data and tests): Chase by
    default, or any other issuer (e.g. "Citi", "American Express"). For a credit
    card, also saves the masked card used to pay. The processor token reference is
    random — never derived from the last four digits. Only a Chase credit card is
    flagged for Chase benefits (marketplace_eligible, a legacy column name)."""
    last4 = last4 or f"{secrets.randbelow(10000):04d}"
    suffix = secrets.token_hex(3).upper()
    issuer_code = cards.normalize_issuer(issuer)
    pm_id = None
    if account_type == "credit_card" and with_payment_method:
        pm_id = f"PM_{network[:4].upper()}_{last4}_{suffix}"
        if is_default:
            db.query(PaymentMethod).filter_by(user_id=user_id).update({"is_default": False})
        db.add(PaymentMethod(payment_method_id=pm_id, user_id=user_id, brand=network, last4=last4,
                             issuer=issuer_code, source="seeded", exp_month=9, exp_year=2030, cardholder_name=None,
                             token_ref=f"tok_{secrets.token_urlsafe(18)}", behaviour=behaviour,
                             is_default=is_default))
    label = f"{issuer} {network} credit card" if account_type == "credit_card" else f"{issuer} {account_type}"
    acct = CardAccount(account_id=f"ACCT_{secrets.token_hex(6).upper()}", user_id=user_id, issuer=issuer,
                       account_type=account_type, product_name=label,
                       network=network if account_type == "credit_card" else None, last4=last4,
                       status="active",
                       marketplace_eligible=eligible and account_type == "credit_card" and issuer_code == "CHASE",
                       agent_pay_capable=(agent_pay_capable if agent_pay_capable is not None else
                                          account_type == "credit_card" and network == "Visa" and issuer_code == "CHASE"),
                       payment_method_id=pm_id,
                       credit_limit=credit_limit if account_type == "credit_card" else None,
                       available_credit=credit_limit if account_type == "credit_card" else None)
    db.add(acct)
    db.commit()
    return acct
