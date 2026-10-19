"""
ShopSphere offers (Part 2; CH-3: every offer is a Chase credit-card benefit).

Deterministic and server-side: which offers a customer can use, which one
applies to a checkout, and what it is worth. The browser and the LLM never send
an offer or an amount; the checkout asks this module, which asks the
OfferEligibilityService (backend/shop/entitlements.py) — customer, selected
payment card and order all have to qualify.

Rules (D41, CH-3):
  * every offer needs an eligible Chase credit card, and the order must be PAID
    with it (decision Q5): another issuer's card, the wallet or "no card yet" → no offer;
  * at most ONE discount offer per checkout — the one that saves the most;
  * bonus-points offers add up (extra points per $1 on matching items), including
    the Chase points boost OFF_CHASE2X (2× points on everything with a Chase card);
  * cashback is a simulated statement credit, only after an approved Chase Visa
    agent payment (VIC path), never a price reduction;
  * max_uses_per_member counts redemptions on paid orders;
  * OFF_STYLE3X needs enrollment (mocked) to show an "enroll to use" case.

These are demo offers. They are not real Chase or Visa programmes.
"""
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.schema import MemberOffer, OfferEnrollment, OfferRedemption, User
from backend.shop import entitlements
from backend.shop.entitlements import DISCOUNT_KINDS, NO_PAYMENT, Payment  # noqa: F401  (re-exported)

CHASE_CARD = dict(requires_issuer="CHASE", requires_payment_with_issuer=True)

OFFERS = [
    dict(offer_id="OFF_RUN15", title="15% off running shoes",
         description="Chase credit-card price on every running shoe in the marketplace when you pay with your Chase card.",
         kind="discount_percent", value=0.15, department="shoes", subcategories="running,trail", **CHASE_CARD),
    dict(offer_id="OFF_AUDIO20", title="$20 off headphones & speakers",
         description="On headphones, earbuds or speakers when those items come to $150 or more, paid with your Chase "
                     "card. Once per customer.",
         kind="discount_amount", value=20.0, department="electronics", subcategories="headphones,earbuds,speakers",
         min_subtotal=150.0, max_uses_per_member=1, **CHASE_CARD),
    dict(offer_id="OFF_STYLE3X", title="3× points on clothing",
         description="Earn 2 bonus points per $1 on clothing, on top of the usual 1 point, with your Chase card. "
                     "Enroll to use.",
         kind="bonus_points", value=2.0, department="clothing", requires_enrollment=True, **CHASE_CARD),
    dict(offer_id="OFF_AGENTPAY10", title="$10 back with Visa agent payments",
         description="A simulated $10 statement credit when you pay $50 or more with your enrolled Chase Visa through "
                     "Talkshop AI agent payments. Once per customer.",
         kind="cashback", value=10.0, min_subtotal=50.0, audience="visa_agent_pay", max_uses_per_member=1,
         requires_network="VISA", requires_payment_path="vic", **CHASE_CARD),
    dict(offer_id="OFF_CHASE2X", title="2× ShopSphere points with your Chase card",
         description="Earn 1 bonus point per $1 on everything, on top of the usual 1 point, when you pay with your "
                     "Chase credit card.",
         kind="bonus_points", value=1.0, **CHASE_CARD),
]
POLICY_FIELDS = ("title", "description", "kind", "value", "department", "subcategories", "min_subtotal", "audience",
                 "max_uses_per_member", "requires_issuer", "requires_payment_with_issuer", "requires_network",
                 "requires_payment_path", "requires_enrollment")
# Demo enrollments (mocked): these Chase personas are enrolled in OFF_STYLE3X;
# Chris (USR007) deliberately isn't, to show "enroll to use".
SEED_ENROLLMENTS = {"OFF_STYLE3X": ("USR001", "USR002", "USR004")}


def seed(db: Session) -> None:
    """Offers are configuration: insert missing ones and keep the rules of existing
    ones in step with OFFERS (so an older demo database picks up CH-3)."""
    for o in OFFERS:
        spec = {"min_subtotal": 0.0, "audience": "chase_card", "department": None, "subcategories": None,
                "max_uses_per_member": None, "requires_issuer": None, "requires_payment_with_issuer": False,
                "requires_network": None, "requires_payment_path": None, "requires_enrollment": False, **o}
        row = db.query(MemberOffer).filter_by(offer_id=o["offer_id"]).first()
        if not row:
            db.add(MemberOffer(**spec))
        else:
            for f in POLICY_FIELDS:
                setattr(row, f, spec[f])
    for offer_id, users in SEED_ENROLLMENTS.items():
        for uid in users:
            if db.query(User).filter_by(user_id=uid).first() and not entitlements.enrolled(db, uid, offer_id):
                db.add(OfferEnrollment(user_id=uid, offer_id=offer_id))
    db.commit()


def uses(db: Session, user_id: str, offer_id: str) -> int:
    return entitlements.uses(db, user_id, offer_id)


def entitled(db: Session, user_id: str) -> list[MemberOffer]:
    """Offers this customer can use at all (customer-level rules: Chase credit card,
    dates, enrollment, limits). Whether they apply to an order also depends on the
    card it is paid with — see best_discount / bonus_points / cashback."""
    return entitlements.customer_offers(db, user_id)


_matches = entitlements._matches
eligible_subtotal = entitlements.eligible_subtotal


def _decisions(db: Session, user_id: str, lines: list[dict], payment: Optional[Payment]) -> list:
    return entitlements.evaluate(db, user_id, lines, payment or NO_PAYMENT)


def best_discount(db: Session, user_id: str, lines: list[dict],
                  payment: Optional[Payment] = None) -> tuple[Optional[MemberOffer], float]:
    """The one discount that saves the most, for this order paid this way (no
    payment given = no card yet = no card-bound offer)."""
    best, amount = None, 0.0
    for d in _decisions(db, user_id, lines, payment):
        if d.eligible and d.offer.kind in DISCOUNT_KINDS and d.amount > amount:
            best, amount = d.offer, d.amount
    return best, amount


def bonus_points(db: Session, user_id: str, lines: list[dict], payment: Optional[Payment] = None) -> list[dict]:
    return [{"offer_id": d.offer.offer_id, "title": d.offer.title, "points": d.points}
            for d in _decisions(db, user_id, lines, payment)
            if d.eligible and d.offer.kind == "bonus_points" and d.points > 0]


def cashback(db: Session, user_id: str, lines: list[dict], payment: Optional[Payment] = None) -> list[dict]:
    """Simulated statement credits this order would earn (Chase Visa agent payment only)."""
    return [{"offer_id": d.offer.offer_id, "title": d.offer.title, "amount": d.amount}
            for d in _decisions(db, user_id, lines, payment) if d.eligible and d.offer.kind == "cashback"]


def offer_view(db: Session, o: MemberOffer, user_id: Optional[str] = None) -> dict:
    left = None
    if user_id and o.max_uses_per_member is not None:
        left = max(0, o.max_uses_per_member - uses(db, user_id, o.offer_id))
    reason = entitlements.customer_reason(db, user_id, o) if user_id else None
    return {"offer_id": o.offer_id, "title": o.title, "description": o.description, "kind": o.kind,
            "value": o.value, "department": o.department,
            "subcategories": o.subcategories.split(",") if o.subcategories else [],
            "min_subtotal": o.min_subtotal, "audience": o.audience, "uses_left": left,
            "label": "Demo offer",
            # CH-3: eligibility decided by the server, with the reason when it doesn't apply
            "exclusive_to": "CHASE" if o.requires_issuer else None,
            "pay_with_chase_card": bool(o.requires_payment_with_issuer),
            "requires_network": o.requires_network, "requires_payment_path": o.requires_payment_path,
            "requires_enrollment": bool(o.requires_enrollment),
            "enrolled": entitlements.enrolled(db, user_id, o.offer_id) if user_id else False,
            "eligible": reason is None, "reason": reason, "reason_text": entitlements.REASONS.get(reason)}


def visible_offers(db: Session, user_id: str) -> list[dict]:
    """The offers page for a Chase credit-card holder: every running Chase offer with
    its customer-level state (eligible, or e.g. NOT_ENROLLED / LIMIT_REACHED).
    Customers without a Chase credit card get [] — offers aren't theirs to see as usable."""
    out = []
    for d in entitlements.evaluate(db, user_id):
        if d.reason in ("NOT_CHASE_CARDHOLDER", "OFFER_NOT_STARTED", "OFFER_EXPIRED"):
            continue
        out.append(offer_view(db, d.offer, user_id))
    return out


def enroll(db: Session, user_id: str, offer_id: str) -> Optional[str]:
    """Mock enrollment. Returns an error code, or None when enrolled (idempotent)."""
    o = db.query(MemberOffer).filter_by(offer_id=offer_id, active=True).first()
    if not o:
        return "UNKNOWN_OFFER"
    if entitlements.customer_reason(db, user_id, o) == "NOT_CHASE_CARDHOLDER":
        return "NOT_CHASE_CARDHOLDER"
    if not entitlements.enrolled(db, user_id, offer_id):
        db.add(OfferEnrollment(user_id=user_id, offer_id=offer_id))
        db.commit()
    return None


def badges_for_product(offers: list[MemberOffer], department: Optional[str], subcategory: Optional[str]) -> list[dict]:
    """Short offer labels for a product card (computed on the server). Every offer
    is a Chase card benefit, so the label says so."""
    out = []
    for o in offers:
        if o.kind == "cashback" or not _matches(o, department, subcategory):
            continue
        out.append({"offer_id": o.offer_id, "kind": o.kind, "title": o.title,
                    "pay_with_chase_card": bool(o.requires_payment_with_issuer),
                    "short": {"discount_percent": f"{int(o.value * 100)}% off",
                              "discount_amount": f"${o.value:.0f} off" + (f" ${o.min_subtotal:.0f}+" if o.min_subtotal else ""),
                              "bonus_points": f"{int(o.value) + 1}× points"}[o.kind]})
    return out


def record(db: Session, user_id: str, order_id: str, offer_id: str, kind: str, amount: float) -> bool:
    """Record one redemption (idempotent: the same offer on the same order once)."""
    if db.query(OfferRedemption).filter_by(offer_id=offer_id, order_id=order_id).first():
        return False
    db.add(OfferRedemption(offer_id=offer_id, user_id=user_id, order_id=order_id, kind=kind, amount=amount))
    return True
