"""
OfferEligibilityService (CH-3) — which offers apply, and why not.

Deterministic, server-side, MOCK rules (no bank is asked). The browser and the
LLM never send an offer, an amount or an eligibility answer; checkout asks here.

Every ShopSphere offer is a Chase credit-card benefit (decision Q4). An offer
applies only when every rule passes, checked in this order:

  customer level (no checkout needed — offers page, product badges)
    NOT_CHASE_CARDHOLDER        requires_issuer=CHASE and no eligible Chase credit card on the account
    OFFER_NOT_STARTED / OFFER_EXPIRED   outside starts_at / ends_at
    NOT_ENROLLED                requires_enrollment and the customer hasn't enrolled
    LIMIT_REACHED               max_uses_per_member used up (paid orders)
    NETWORK_PATH_REQUIRED       requires_network (e.g. VISA) and no eligible Chase card on that network
  payment level (a checkout and its SELECTED way to pay — decision Q5)
    PAYMENT_CARD_NOT_ELIGIBLE   requires_payment_with_issuer and the order isn't paid with an
                                eligible Chase credit card (another issuer's card, the wallet, no card yet)
    NETWORK_PATH_REQUIRED       requires_network / requires_payment_path (e.g. Visa agent payment) not met
  order level
    NO_MATCHING_ITEMS / BELOW_MINIMUM   the order's items don't qualify

Being a Chase cardholder is therefore never assumed to unlock every Chase offer:
each offer is checked against the customer, the selected card and the order.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.schema import CardAccount, Checkout, MemberOffer, OfferEnrollment, OfferRedemption, PaymentMethod, Product
from backend.shop import cards

DISCOUNT_KINDS = ("discount_percent", "discount_amount")

REASONS = {
    "NOT_CHASE_CARDHOLDER": "Offers are for Chase credit-card holders.",
    "OFFER_NOT_STARTED": "This offer hasn't started yet.",
    "OFFER_EXPIRED": "This offer has ended.",
    "NOT_ENROLLED": "Enroll in this offer to use it.",
    "LIMIT_REACHED": "You've already used this offer.",
    "NETWORK_PATH_REQUIRED": "This offer needs a Chase Visa paid through Visa agent payments.",
    "PAYMENT_CARD_NOT_ELIGIBLE": "Pay with your Chase credit card to get this offer.",
    "NO_MATCHING_ITEMS": "Nothing in this order qualifies.",
    "BELOW_MINIMUM": "The qualifying items are below this offer's minimum.",
}


@dataclass
class Payment:
    """How a checkout will be paid: "card" (a saved/selected card), "wallet", or "none"."""
    kind: str = "none"
    payment_method_id: Optional[str] = None
    path: Optional[str] = None          # vic | vic_setup_required | standard | wallet | none


NO_PAYMENT = Payment()


@dataclass
class Decision:
    offer: MemberOffer
    eligible: bool
    reason: Optional[str] = None
    base: float = 0.0                   # $ of the order this offer applies to
    amount: float = 0.0                 # $ discount or $ cashback
    points: int = 0                     # bonus points
    extras: dict = field(default_factory=dict)

    @property
    def reason_text(self) -> Optional[str]:
        return REASONS.get(self.reason) if self.reason else None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def chase_card_accounts(db: Session, user_id: str) -> list[CardAccount]:
    from backend.shop.eligibility import chase_cards
    return chase_cards(db, user_id)


def payment_for_checkout(db: Session, co: Checkout, path: Optional[str] = None) -> Payment:
    if (co.pay_with or "card") == "wallet":
        return Payment(kind="wallet", path="wallet")
    if co.pay_with == "provider":                 # CH-6: the card is unknown until after approval → no
        return Payment(kind="provider", path="provider_handoff")   # card-bound Chase benefit (decision Q10)
    if not co.payment_method_id:
        return Payment(kind="none", path="none")
    if path is None:
        from backend.shop import vic
        path = vic.payment_path(db, co)["path"]
    return Payment(kind="card", payment_method_id=co.payment_method_id, path=path)


def chase_card_for(db: Session, user_id: str, payment_method_id: Optional[str]) -> Optional[CardAccount]:
    """The eligible Chase credit-card account behind this saved card, if it is one."""
    if not payment_method_id:
        return None
    pm = db.query(PaymentMethod).filter_by(payment_method_id=payment_method_id, user_id=user_id).first()
    if not pm or (pm.status or "active") != "active":
        return None
    return next((a for a in chase_card_accounts(db, user_id) if a.payment_method_id == payment_method_id), None)


def enrolled(db: Session, user_id: str, offer_id: str) -> bool:
    return db.query(OfferEnrollment).filter_by(user_id=user_id, offer_id=offer_id).first() is not None


def uses(db: Session, user_id: str, offer_id: str) -> int:
    return db.query(OfferRedemption).filter_by(user_id=user_id, offer_id=offer_id).count()


def customer_reason(db: Session, user_id: str, o: MemberOffer, *, now: Optional[datetime] = None) -> Optional[str]:
    """Why this customer can't use the offer at all (None = can, subject to the payment and order)."""
    now = now or _now()
    chase = chase_card_accounts(db, user_id)
    if cards.normalize_issuer(o.requires_issuer) == "CHASE" and not chase:
        return "NOT_CHASE_CARDHOLDER"
    if o.starts_at and now < o.starts_at:
        return "OFFER_NOT_STARTED"
    if o.ends_at and now > o.ends_at:
        return "OFFER_EXPIRED"
    if o.requires_enrollment and not enrolled(db, user_id, o.offer_id):
        return "NOT_ENROLLED"
    if o.max_uses_per_member is not None and uses(db, user_id, o.offer_id) >= o.max_uses_per_member:
        return "LIMIT_REACHED"
    if o.requires_network and not any(cards.normalize_network(a.network) == o.requires_network for a in chase):
        return "NETWORK_PATH_REQUIRED"
    return None


def payment_reason(db: Session, user_id: str, o: MemberOffer, payment: Payment) -> Optional[str]:
    """Why the way this order is paid doesn't qualify (None = it does)."""
    if not (o.requires_payment_with_issuer or o.requires_network or o.requires_payment_path):
        return None
    acct = chase_card_for(db, user_id, payment.payment_method_id) if payment.kind == "card" else None
    if o.requires_payment_with_issuer and acct is None:
        return "PAYMENT_CARD_NOT_ELIGIBLE"
    if o.requires_network and (acct is None or cards.normalize_network(acct.network) != o.requires_network):
        return "NETWORK_PATH_REQUIRED"
    if o.requires_payment_path and payment.path != o.requires_payment_path:
        return "NETWORK_PATH_REQUIRED"
    return None


def _matches(o: MemberOffer, department: Optional[str], subcategory: Optional[str]) -> bool:
    if o.department and o.department != department:
        return False
    if o.subcategories and subcategory not in o.subcategories.split(","):
        return False
    return True


def _line_scope(db: Session, ln: dict) -> tuple[Optional[str], Optional[str]]:
    p = db.query(Product).filter_by(product_id=ln["product_id"]).first()
    return (p.department, p.subcategory) if p else (None, None)


def eligible_subtotal(db: Session, o: MemberOffer, lines: list[dict]) -> float:
    return round(sum(ln["unit_price"] * ln["quantity"] for ln in lines if _matches(o, *_line_scope(db, ln))), 2)


def _value(o: MemberOffer, base: float, subtotal: float) -> tuple[float, int]:
    """(dollar amount, bonus points) the offer is worth on this base."""
    if o.kind == "discount_percent":
        return round(base * o.value, 2), 0
    if o.kind == "discount_amount":
        return round(min(o.value, base), 2), 0
    if o.kind == "bonus_points":
        return 0.0, int(base * o.value)
    if o.kind == "cashback":
        return round(o.value, 2), 0
    return 0.0, 0


def evaluate(db: Session, user_id: str, lines: Optional[list[dict]] = None,
             payment: Optional[Payment] = None) -> list[Decision]:
    """Every active offer, with a decision. Without lines/payment only the
    customer-level rules are checked (offers page, product badges)."""
    out = []
    for o in db.query(MemberOffer).filter_by(active=True).order_by(MemberOffer.id):
        reason = customer_reason(db, user_id, o)
        if reason is None and payment is not None:
            reason = payment_reason(db, user_id, o, payment)
        d = Decision(offer=o, eligible=reason is None, reason=reason)
        if reason is None and lines is not None:
            subtotal = round(sum(ln["unit_price"] * ln["quantity"] for ln in lines), 2)
            base = subtotal if o.kind == "cashback" else eligible_subtotal(db, o, lines)
            if base <= 0:
                d.eligible, d.reason = False, "NO_MATCHING_ITEMS"
            elif base < (o.min_subtotal or 0):
                d.eligible, d.reason = False, "BELOW_MINIMUM"
            else:
                d.base = base
                d.amount, d.points = _value(o, base, subtotal)
        out.append(d)
    return out


PAYMENT_REASONS = ("PAYMENT_CARD_NOT_ELIGIBLE", "NETWORK_PATH_REQUIRED")


def benefit_summary(db: Session, user_id: str, lines: list[dict], payment: Payment) -> dict:
    """CH-4: what this order gets with the selected way to pay, and which Chase
    benefits it would get with an eligible Chase card but doesn't (removed, with the
    reason). Only customer-eligible offers can be "removed": a non-Chase customer
    sees no list of Chase benefits they could never use."""
    decisions = evaluate(db, user_id, lines, payment)
    best = max((d for d in decisions if d.eligible and d.offer.kind in DISCOUNT_KINDS),
               key=lambda d: d.amount, default=None)
    applied, removed = [], []
    for d in decisions:
        o = d.offer
        if d.eligible:
            if o.kind in DISCOUNT_KINDS and d is not best:
                continue                                        # one discount per order: the best one
            applied.append({"offer_id": o.offer_id, "title": o.title, "kind": o.kind,
                            "amount": d.amount, "points": d.points})
        elif d.reason in PAYMENT_REASONS and customer_reason(db, user_id, o) is None:
            base = (round(sum(ln["unit_price"] * ln["quantity"] for ln in lines), 2) if o.kind == "cashback"
                    else eligible_subtotal(db, o, lines))
            if base > 0 and base >= (o.min_subtotal or 0):
                amount, points = _value(o, base, base)
                removed.append({"offer_id": o.offer_id, "title": o.title, "kind": o.kind, "amount": amount,
                                "points": points, "reason": d.reason, "reason_text": d.reason_text})
    return {"applied": applied, "removed": removed}


def customer_offers(db: Session, user_id: str) -> list[MemberOffer]:
    """Offers this customer can use at all (customer-level rules passed)."""
    return [d.offer for d in evaluate(db, user_id) if d.eligible]
