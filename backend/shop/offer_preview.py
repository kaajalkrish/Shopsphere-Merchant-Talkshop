"""
Discount discovery without checkout (MCP get_available_offers / preview_cart_discounts).

Read-only answers to "are there any offers?" and "what would my discount be?", from the same
offer rules and OfferEligibilityService the checkout uses (backend/shop/offers.py,
backend/shop/entitlements.py). Nothing here creates a checkout or a payment session, and nothing
asks for an address, a card or a sign-in.

Each offer comes back as one of:
  public            no conditions at all (applies to anyone; ShopSphere has none today)
  conditional       real conditions the shopper hasn't been verified for (anonymous visitors and
                    guests; a customer without the required card) — with the conditions and what
                    it WOULD be worth on these items if they're met
  verified          a signed-in customer the engine checked: their account and one of their saved
                    cards qualify (the estimate assumes they pay with that card)
  not_eligible      a signed-in customer the engine checked who can't use it, with the reason

Estimates use the live catalog prices; the final amount is recalculated at checkout once the way
to pay is chosen (one discount per order: the one that saves the most). Guest checkout gets no
Chase offers: they are card benefits of a ShopSphere account with an eligible Chase credit card.
All offers are demo offers, not real Chase or Visa programmes.
"""
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.schema import MemberOffer, Product
from backend.shop import entitlements
from backend.shop.entitlements import DISCOUNT_KINDS, Payment

LABEL = "Demo offer (simulated; not a real Chase or Visa programme)"


def _scope_text(o: MemberOffer) -> str:
    subs = [s.replace("_", " ") for s in (o.subcategories or "").split(",") if s]
    if subs:
        return f"{' and '.join(subs)} {o.department or 'items'}"
    return o.department or "everything"


def conditions(o: MemberOffer) -> list[str]:
    out = []
    if entitlements.cards.normalize_issuer(o.requires_issuer) == "CHASE":
        out.append("a ShopSphere account with an eligible Chase credit card saved on it"
                   + (", and the order paid with that card" if o.requires_payment_with_issuer else ""))
    if o.requires_network:
        out.append(f"the Chase card is on the {o.requires_network.title()} network")
    if o.requires_payment_path == "vic":
        out.append("paid through Visa agent payments in Talkshop")
    if o.min_subtotal:
        out.append(f"qualifying items total ${o.min_subtotal:.0f} or more")
    if o.requires_enrollment:
        out.append("enrollment in the offer")
    if o.max_uses_per_member:
        out.append("once per customer" if o.max_uses_per_member == 1 else f"up to {o.max_uses_per_member} uses")
    if o.ends_at:
        out.append(f"until {o.ends_at.date().isoformat()}")
    return out


def _is_public(o: MemberOffer) -> bool:
    return not (o.requires_issuer or o.requires_payment_with_issuer or o.requires_network
                or o.requires_payment_path or o.requires_enrollment)


def _worth(db: Session, o: MemberOffer, lines: list[dict]) -> tuple[float, float, int, Optional[str]]:
    """(qualifying subtotal, $ amount, bonus points, problem) on these lines, ignoring who pays."""
    subtotal = round(sum(ln["unit_price"] * ln["quantity"] for ln in lines), 2)
    base = subtotal if o.kind == "cashback" else entitlements.eligible_subtotal(db, o, lines)
    if base <= 0:
        return 0.0, 0.0, 0, "NO_MATCHING_ITEMS"
    if base < (o.min_subtotal or 0):
        return base, 0.0, 0, "BELOW_MINIMUM"
    amount, points = entitlements._value(o, base, subtotal)
    return base, amount, points, None


def _chase_payments(db: Session, user_id: str) -> list[Payment]:
    return [Payment(kind="card", payment_method_id=a.payment_method_id, path="standard")
            for a in entitlements.chase_card_accounts(db, user_id) if a.payment_method_id]


def _card_label(db: Session, user_id: str, pm_id: str) -> str:
    from backend.db.schema import PaymentMethod
    pm = db.query(PaymentMethod).filter_by(payment_method_id=pm_id, user_id=user_id).first()
    return f"{pm.brand} •••• {pm.last4}" if pm else "your Chase card"


def preview(db: Session, lines: list[dict], user_id: Optional[str] = None, signed_in: bool = False) -> dict:
    """Offers for these lines (cart lines or one product as a line). user_id + signed_in=True: the
    engine checks this customer; otherwise nothing about the shopper is assumed."""
    offers_out, best = [], None
    for o in db.query(MemberOffer).filter_by(active=True).order_by(MemberOffer.id):
        base, amount, points, problem = _worth(db, o, lines) if lines else (0.0, 0.0, 0, None)
        if problem == "NO_MATCHING_ITEMS":
            continue                                           # not about these items
        item = {"offer_id": o.offer_id, "title": o.title, "kind": o.kind, "applies_to": _scope_text(o),
                "conditions": conditions(o), "qualifying_subtotal": base, "label": LABEL,
                "estimate": None if not lines else {"amount": amount, "bonus_points": points,
                             "is_price_reduction": o.kind in DISCOUNT_KINDS,
                             "note": "simulated statement credit after payment, not a price reduction"
                             if o.kind == "cashback" else None},
                "guest_checkout_eligible": False if o.requires_issuer else True}
        if problem == "BELOW_MINIMUM":
            item.update(type="conditional", status="below_minimum",
                        explanation=f"These items come to ${base:.2f}; the offer needs ${o.min_subtotal:.0f}+.")
        elif _is_public(o):
            item.update(type="public", status="applies")
        elif not signed_in or not user_id:
            item.update(type="conditional", status="not_verified",
                        explanation="Eligibility isn't verified: it depends on the conditions listed. ShopSphere "
                                    "doesn't know your cards unless you sign in.")
        else:
            reason = entitlements.customer_reason(db, user_id, o)
            pay_ok = None
            if reason is None:                                 # one of their own Chase cards, paid the required way
                for pay in _chase_payments(db, user_id):
                    test = Payment(kind="card", payment_method_id=pay.payment_method_id,
                                   path=o.requires_payment_path or "standard")
                    if entitlements.payment_reason(db, user_id, o, test) is None:
                        pay_ok = test
                        break
            if reason is None and pay_ok is not None:
                card = _card_label(db, user_id, pay_ok.payment_method_id)
                item.update(type="verified", status="eligible", pay_with=card,
                            explanation=f"Your account qualifies when you pay with {card}"
                                        + (" through Visa agent payments" if o.requires_payment_path else "")
                                        + ". Final amount is confirmed at checkout.")
            else:
                why = reason or "PAYMENT_CARD_NOT_ELIGIBLE"
                item.update(type="not_eligible", status=why.lower(), reason=why,
                            explanation=entitlements.REASONS.get(why),
                            estimate={**item["estimate"], "amount": 0.0, "bonus_points": 0} if lines else None)
        offers_out.append(item)
        if (o.kind in DISCOUNT_KINDS and item["status"] in ("applies", "eligible", "not_verified")
                and amount > (best or {}).get("amount", 0)):
            best = {"offer_id": o.offer_id, "title": o.title, "amount": amount,
                    "verified": item["status"] in ("applies", "eligible")}
    subtotal = round(sum(ln["unit_price"] * ln["quantity"] for ln in lines), 2)
    return {
        "offers": offers_out, "subtotal": subtotal,
        "best_discount_estimate": best,
        "estimated_subtotal_after_discount": round(subtotal - best["amount"], 2) if best else subtotal,
        "eligibility_verified": bool(signed_in and user_id),
        "one_discount_per_order": True,
        "final_amount_note": "Estimates only. ShopSphere recalculates offers at checkout when the payment method is "
                             "chosen; tax and shipping are added there.",
        "guest_checkout_note": "Guest checkout doesn't get Chase offers: they're benefits of a ShopSphere account "
                               "with an eligible Chase credit card, paid with that card.",
    }


def product_line(db: Session, product_id: str) -> Optional[dict]:
    p = db.query(Product).filter_by(product_id=product_id).first()
    if not p:
        return None
    return {"product_id": p.product_id, "name": p.name, "unit_price": float(p.price or 0), "quantity": 1}
