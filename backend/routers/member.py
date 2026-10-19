"""
Customer + Chase-benefits API (CH-2). No AI in any endpoint.

  GET /api/member     who am I: access (any signed-in customer), whether I hold a
                      credit card, whether Chase benefits apply, masked accounts,
                      points, agent-pay status. Open to any session.
  GET /api/offers     offers I'm entitled to — Chase credit-card holders only
                      (decision Q4: every offer is Chase-exclusive). Signed-in customers.
  GET /api/rewards    ShopSphere points summary + ledger (every signed-in customer)

  POST /api/auth/register-cardmember   {full_name, email, password, last4}
        Sign up AND link Chase card benefits: name + email + last four digits must
        match one record in the SIMULATED Chase cardholder directory. Optional —
        plain sign-up is POST /api/auth/register. Never an access requirement.
  POST /api/member/verify-card         {last4}
        A signed-in customer links a Chase card for Chase benefits: the account's
        own name + email + these digits must match. The last four digits are a
        matching factor here, not proof of card ownership.
"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

from backend.auth.dependencies import CurrentUser, get_current_user
from backend.auth.security import create_access_token, hash_password
from backend.db.init_db import STARTING_WALLET_BALANCE
from backend.db.schema import CardAccount, LoyaltyPoints, User, Wallet
from backend.db.session_utils import get_session, write_audit_event
from backend.shop import cardholder_verification as verification
from backend.shop import eligibility, offers, rewards
from backend.shop.eligibility import require_shopper

router = APIRouter(tags=["member"])


def _primary_card(db, user_id: str):
    """The card shown in the header: a Chase credit card first (Chase benefits),
    else the customer's other active credit-card account (e.g. Citi, Amex)."""
    chase = eligibility.chase_cards(db, user_id)
    if chase:
        return chase[0]
    return (db.query(CardAccount).filter_by(user_id=user_id, account_type="credit_card", status="active")
            .order_by(CardAccount.id).first())


def member_summary(db, user: CurrentUser) -> dict:
    """CH-2: "eligible" now means *access* (a signed-in customer, any issuer) — the
    UI keys off it. Chase status is reported separately as an entitlement."""
    code = eligibility.check_access(db, user)
    signed_in = not user.is_visitor
    holder = signed_in and eligibility.is_credit_card_holder(db, user.user_id)
    chase = signed_in and eligibility.has_chase_relationship(db, user.user_id)
    out = {"signed_in": signed_in, "eligible": code is None, "code": code,
           "message": eligibility.MESSAGES.get(code) if code else None,
           "access": {"allowed": code is None, "code": code},
           "credit_card_holder": holder,
           "chase_benefits": chase,
           "chase": {"linked": chase,
                     "eligible_card_count": len(eligibility.chase_cards(db, user.user_id)) if signed_in else 0},
           "member": None}
    if not signed_in:
        return out
    row = db.query(User).filter_by(user_id=user.user_id).first()
    lp = db.query(LoyaltyPoints).filter_by(user_id=user.user_id).first()
    primary = _primary_card(db, user.user_id)
    out["member"] = {
        "name": user.name,
        "member_since": row.created_at.date().isoformat() if row and row.created_at else None,
        "status": ("Chase credit-card holder" if chase else
                   "ShopSphere customer" if holder else "ShopSphere customer · no credit card yet"),
        "accounts": eligibility.accounts(db, user.user_id),
        "primary_card": eligibility.card_view(primary) if primary else None,
        "rewards_balance": lp.balance if lp else 0,
    }
    if code is None:
        from backend.routers.vic import status_view
        out["member"]["agent_pay"] = status_view(db, user.user_id)["cards"]
    return out


@router.get("/api/member")
def get_member(user: CurrentUser = Depends(get_current_user)):
    with get_session() as db:
        return member_summary(db, user)


@router.get("/api/offers")
def get_offers(user: CurrentUser = Depends(require_shopper)):
    """Chase credit-card holders see their offers, each with its state (eligible, or
    e.g. NOT_ENROLLED); everyone else gets [] (offers are a Chase benefit, Q4).
    Whether an offer applies to an order also depends on the card it's paid with."""
    with get_session() as db:
        return offers.visible_offers(db, user.user_id)


@router.post("/api/offers/{offer_id}/enroll")
def enroll_offer(offer_id: str, user: CurrentUser = Depends(require_shopper)):
    """Mock offer enrollment (CH-3) for Chase credit-card holders."""
    with get_session() as db:
        code = offers.enroll(db, user.user_id, offer_id)
        if code:
            status = 404 if code == "UNKNOWN_OFFER" else 403
            message = "No such offer." if code == "UNKNOWN_OFFER" else offers.entitlements.REASONS[code]
            return JSONResponse(status_code=status, content={"detail": {"code": code, "message": message}})
        write_audit_event(db, "OFFER_ENROLLED", user_id=user.user_id, metadata={"offer_id": offer_id, "mock": True})
        db.commit()
        return offers.visible_offers(db, user.user_id)


@router.get("/api/rewards")
def get_rewards(user: CurrentUser = Depends(require_shopper)):
    with get_session() as db:
        return {**rewards.summary(db, user.user_id), "ledger": rewards.ledger(db, user.user_id)}


# ── Sign up as a cardmember / verify a card (simulated issuer directory) ─────

class CardmemberSignup(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=200)
    last4: str = Field(..., min_length=4, max_length=4)


class VerifyCard(BaseModel):
    last4: str = Field(..., min_length=4, max_length=4)


def _refused(exc: verification.VerificationError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "message": exc.message}})


@router.post("/api/auth/register-cardmember")
def register_cardmember(req: CardmemberSignup, authorization: Optional[str] = Header(default=None)):
    from backend.routers.auth import AuthResponse, _adopt_visitor, _visitor_id
    email = req.email.strip().lower()
    with get_session() as db:
        if db.query(User).filter(User.email == email).first():
            return JSONResponse(status_code=409, content={"detail": {
                "code": "EMAIL_EXISTS",
                "message": "An account with this email already exists. Sign in, then link your Chase card."}})
        try:
            records = verification.match(db, req.full_name, email, req.last4)
        except verification.VerificationError as exc:
            return _refused(exc)
        user_id = f"USR{uuid.uuid4().hex[:6].upper()}"
        name = " ".join(req.full_name.split())
        db.add(User(user_id=user_id, name=name, email=email, password_hash=hash_password(req.password), status="active"))
        db.add(Wallet(user_id=user_id, balance=STARTING_WALLET_BALANCE))
        db.add(LoyaltyPoints(user_id=user_id, balance=0, lifetime_points=0))
        linked = verification.link(db, user_id, records, name)
        write_audit_event(db, "CARDMEMBER_SIGNUP", user_id=user_id,
                          metadata={"linked": [verification.view(a) for a in linked], "issuer": "Chase (simulated)"})
        db.commit()
    current = CurrentUser(user_id=user_id, name=name, email=email)
    merged = _adopt_visitor(_visitor_id(authorization), user_id)
    return AuthResponse(access_token=create_access_token(user_id), user=current, merged_lines=merged)


@router.post("/api/member/verify-card")
def verify_card(req: VerifyCard, user: CurrentUser = Depends(get_current_user)):
    if user.is_visitor:
        return JSONResponse(status_code=403, content={"detail": {
            "code": "LOGIN_REQUIRED", "message": "Sign in first, then link your Chase card."}})
    with get_session() as db:
        try:
            records = verification.match(db, user.name, user.email, req.last4)
        except verification.VerificationError as exc:
            return _refused(exc)
        linked = verification.link(db, user.user_id, records, user.name)
        write_audit_event(db, "CARDMEMBER_VERIFIED", user_id=user.user_id,
                          metadata={"linked": [verification.view(a) for a in linked], "issuer": "Chase (simulated)"})
        db.commit()
        return member_summary(db, user)

