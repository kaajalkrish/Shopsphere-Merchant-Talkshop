"""
Checkout — builds the one canonical checkout snapshot the customer reviews.

Everything that decides the charge lives here on the server: lines at live
catalog prices, the shipping address, delivery option and date, tax, total,
and the saved card. The browser or Talkshop can ask for changes (quantity,
delivery, address, card) but never sends amounts; confirm() charges exactly
what this module last calculated and hashed.
"""
import hashlib
import re
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.schema import (Address, CartItem, Checkout, LoyaltyPoints, PaymentMethod, Product, ProductVariant,
                               User, Wallet)
from backend.payment.policy import TAX_RATE
from backend.shop import offers
from backend.shop.profile import address_dict, card_dict, list_addresses, list_cards

EXPRESS_PRICE = 9.99
# CH-6: paying with a new card on the payment provider's own page (no card number reaches ShopSphere)
PROVIDER_VIEW = {"payment_method_id": "provider", "type": "provider",
                 "display": "New card · Simulated Secure Payment Provider"}
EXPRESS_DAYS = 2
DELIVERY_LABELS = {"standard": "Standard", "express": "Express"}


class CheckoutError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, detail: Optional[dict] = None):
        super().__init__(message)
        self.code, self.message, self.status, self.detail = code, message, status, detail or {}


def _today() -> date:
    return datetime.now(timezone.utc).date()


def delivery_dates(standard_days: int) -> dict[str, date]:
    today = _today()
    express_days = max(1, min(EXPRESS_DAYS, standard_days - 1))
    return {"standard": today + timedelta(days=standard_days), "express": today + timedelta(days=express_days)}


def _lines(co: Checkout) -> list[dict]:
    return json.loads(co.lines_json)


def _hash(co: Checkout, lines: list[dict]) -> str:
    canonical = {
        "checkout_id": co.checkout_id, "user_id": co.user_id,
        "lines": [[ln["sku"], ln["quantity"], ln["unit_price"]] for ln in lines],
        "address_id": co.address_id, "delivery_method": co.delivery_method,
        "payment_method_id": co.payment_method_id, "pay_with": co.pay_with or "card",
        "points_used": co.points_used or 0, "points_discount": co.points_discount or 0.0,
        "offer_id": co.offer_id, "offer_discount": co.offer_discount or 0.0,
        "guest_email": co.guest_email,
        "subtotal": co.subtotal, "tax": co.tax, "shipping": co.shipping, "total": co.total, "currency": co.currency,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def _recalculate(db: Session, co: Checkout) -> list[dict]:
    """Refresh lines from the live catalog and recompute every amount + the hash.
    Returns the stock issues found (empty list = all lines can be fulfilled)."""
    lines, issues = _lines(co), []
    for ln in lines:
        product = db.query(Product).filter_by(product_id=ln["product_id"]).first()
        variant = db.query(ProductVariant).filter_by(sku=ln["sku"]).first()
        if product:
            ln["unit_price"] = product.price
            ln["delivery_days"] = product.delivery_days
        ln["line_total"] = round(ln["unit_price"] * ln["quantity"], 2)
        ln["stock"] = variant.stock if variant else 0
        if ln["stock"] < ln["quantity"]:
            issues.append({"code": "OUT_OF_STOCK", "sku": ln["sku"], "name": ln["name"],
                           "requested": ln["quantity"], "available": ln["stock"]})
    co.lines_json = json.dumps(lines)
    co.subtotal = round(sum(ln["line_total"] for ln in lines), 2)
    # Chase offer (Part 2; CH-3): the server picks the one discount that saves the most,
    # and only when this order is paid with an eligible Chase credit card (decision Q5).
    from backend.shop.entitlements import benefit_summary, payment_for_checkout
    paying = payment_for_checkout(db, co)
    offer, offer_discount = offers.best_discount(db, co.user_id, lines, paying)
    # CH-4: record what the selected card gets, and which Chase benefits it would only get with a Chase card
    co.benefits_json = json.dumps(benefit_summary(db, co.user_id, lines, paying))
    co.offer_id, co.offer_discount = (offer.offer_id if offer else None), offer_discount
    after_offer = round(co.subtotal - offer_discount, 2)
    # Loyalty points (Phase 12): 100 points = $1 off the products, before tax.
    co.points_used = min(co.points_used or 0, max_points(db, co.user_id, after_offer))
    co.points_discount = round(co.points_used * POINT_VALUE, 2)
    co.tax = round((after_offer - co.points_discount) * TAX_RATE, 2)
    co.shipping = EXPRESS_PRICE if co.delivery_method == "express" else 0.0
    co.total = round(after_offer - co.points_discount + co.tax + co.shipping, 2)
    std_days = max((ln.get("delivery_days") or 5) for ln in lines)
    co.delivery_date = datetime.combine(delivery_dates(std_days)[co.delivery_method], datetime.min.time())
    new_hash = _hash(co, lines)
    if co.checkout_hash and new_hash != co.checkout_hash:
        co.version = (co.version or 1) + 1                 # CH-4: anything that changes the charge = a new version
    co.checkout_hash = new_hash
    return issues


POINT_VALUE = 0.01          # 100 points = $1
MIN_PRODUCT_PAYABLE = 1.00  # points never make the products free


def points_balance(db: Session, user_id: str) -> int:
    lp = db.query(LoyaltyPoints).filter_by(user_id=user_id).first()
    return int(lp.balance) if lp and not is_guest_owner(db, user_id) else 0


def max_points(db: Session, user_id: str, subtotal: float) -> int:
    """The most points this order can take: what the customer has, leaving at
    least $1 of products to pay. Guests have none."""
    cap = int(round((subtotal - MIN_PRODUCT_PAYABLE) / POINT_VALUE)) if subtotal > MIN_PRODUCT_PAYABLE else 0
    return max(0, min(points_balance(db, user_id), cap))


def is_guest_owner(db: Session, user_id: str) -> bool:
    """A checkout owned by an anonymous visitor session is a guest checkout."""
    u = db.query(User).filter_by(user_id=user_id).first()
    return bool(u and u.is_guest)


def create_checkout(db: Session, user_id: str, line_ids: list[str],
                    quantities: Optional[dict[str, int]] = None) -> Checkout:
    """A checkout for the chosen cart lines (the website's selected items, or
    the item(s) Talkshop added in this conversation). `quantities` buys fewer
    than the cart holds — Talkshop buys what it added in this chat, even if
    the same item was already in the cart."""
    if not line_ids:
        raise CheckoutError("NO_ITEMS", "Choose at least one item to check out.")
    items = db.query(CartItem).filter(CartItem.user_id == user_id, CartItem.cart_item_id.in_(line_ids)).all()
    if len(items) != len(set(line_ids)):
        raise CheckoutError("UNKNOWN_LINE", "Some of those items aren't in your cart.", 404)
    if any(it.sku is None for it in items):
        raise CheckoutError("NO_SKU", "Pick a size and colour for every item before checkout.")
    lines = []
    for it in items:
        product = db.query(Product).filter_by(product_id=it.product_id).first()
        variant = db.query(ProductVariant).filter_by(sku=it.sku).first()
        lines.append({
            "line_id": it.cart_item_id, "sku": it.sku, "product_id": it.product_id,
            "name": product.name if product else it.title, "brand": it.brand,
            "size": it.size, "color": it.color,
            "option_label": (product.option_label if product else None) or "Size",
            "quantity": max(1, min(it.quantity, (quantities or {}).get(it.cart_item_id, it.quantity))),
            "unit_price": product.price if product else it.price,
            "image_url": (variant.image_url if variant else None) or it.image_url,
            "delivery_days": product.delivery_days if product else it.delivery_days,
        })
    addresses, cards = list_addresses(db, user_id), list_cards(db, user_id)
    co = Checkout(
        checkout_id=f"CHK_{uuid.uuid4().hex[:10].upper()}", user_id=user_id, status="open",
        lines_json=json.dumps(lines), cart_item_ids=",".join(it.cart_item_id for it in items),
        address_id=addresses[0].address_id if addresses else None,
        payment_method_id=cards[0].payment_method_id if cards else None,
        delivery_method="standard", currency="USD", pay_with="card", version=1,
    )
    _recalculate(db, co)
    db.add(co)
    db.commit()
    return co


def get_checkout(db: Session, user_id: str, checkout_id: str) -> Checkout:
    co = db.query(Checkout).filter_by(checkout_id=checkout_id, user_id=user_id).first()
    if not co:
        raise CheckoutError("UNKNOWN_CHECKOUT", "Checkout not found.", 404)
    return co


def update_checkout(db: Session, user_id: str, checkout_id: str, *, delivery_method: Optional[str] = None,
                    address_id: Optional[str] = None, payment_method_id: Optional[str] = None,
                    pay_with: Optional[str] = None, points_used: Optional[int] = None,
                    quantities: Optional[dict[str, int]] = None) -> Checkout:
    """Changes made on the review screen. Only an open checkout can change."""
    co = get_checkout(db, user_id, checkout_id)
    if co.status != "open":
        raise CheckoutError("CHECKOUT_CLOSED", f"This checkout is {co.status} and can't be changed.", 409)
    if delivery_method is not None:
        if delivery_method not in DELIVERY_LABELS:
            raise CheckoutError("INVALID_DELIVERY", "Delivery must be standard or express.")
        co.delivery_method = delivery_method
    if address_id is not None:
        if not db.query(Address).filter_by(address_id=address_id, user_id=user_id).first():
            raise CheckoutError("UNKNOWN_ADDRESS", "That address isn't saved on your account.", 404)
        co.address_id = address_id
    if payment_method_id is not None:
        if not db.query(PaymentMethod).filter_by(payment_method_id=payment_method_id, user_id=user_id).first():
            raise CheckoutError("UNKNOWN_CARD", "That card isn't saved on your account.", 404)
        co.payment_method_id = payment_method_id
        co.pay_with = "card"
    if pay_with is not None:
        if pay_with not in ("card", "wallet", "provider"):
            raise CheckoutError("INVALID_PAYMENT", "Pay with a saved card, a new card through the secure payment "
                                                   "provider, or your ShopSphere Wallet.")
        if pay_with == "provider" and is_guest_owner(db, user_id):
            raise CheckoutError("LOGIN_REQUIRED", "Sign in to pay with a new card.", 403)
        if pay_with == "wallet" and (is_guest_owner(db, user_id)
                                     or not db.query(Wallet).filter_by(user_id=user_id).first()):
            raise CheckoutError("WALLET_UNAVAILABLE", "The ShopSphere Wallet is for customers with an account.", 403)
        co.pay_with = pay_with
    if points_used is not None:
        if is_guest_owner(db, user_id):
            raise CheckoutError("POINTS_UNAVAILABLE", "Loyalty points are for customers with an account.", 403)
        if int(points_used) < 0:
            raise CheckoutError("INVALID_POINTS", "Points can't be negative.")
        co.points_used = int(points_used)          # capped to the balance and order in _recalculate
    if quantities:
        lines = _lines(co)
        by_id = {ln["line_id"]: ln for ln in lines}
        for line_id, qty in quantities.items():
            if line_id not in by_id:
                raise CheckoutError("UNKNOWN_LINE", "That item isn't part of this checkout.", 404)
            if qty < 1:
                raise CheckoutError("INVALID_QUANTITY", "Quantity must be at least 1.")
            by_id[line_id]["quantity"] = qty
        co.lines_json = json.dumps(lines)
    _recalculate(db, co)
    _invalidate_instructions(db, co)
    db.commit()
    return co


def _invalidate_instructions(db: Session, co: Checkout) -> None:
    """Part 2: a Visa payment instruction covers one exact set of terms. If the
    checkout's fingerprint changed (card, address, delivery, quantity, points,
    price), any open instruction for the old terms is cancelled."""
    from backend.vic.instructions import invalidate_for_checkout
    invalidate_for_checkout(db, co.checkout_id, co.checkout_hash if co.status == "open" else None)


def reassign(db: Session, user_id: str, field: str, old_id: str, new_id: Optional[str]) -> None:
    """A saved address/card was deleted: open checkouts that used it switch to
    the customer's default (or to none, so checkout asks for one again)."""
    for co in db.query(Checkout).filter_by(user_id=user_id, status="open").filter(getattr(Checkout, field) == old_id):
        setattr(co, field, new_id)
        _recalculate(db, co)
        _invalidate_instructions(db, co)
    db.commit()


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


def set_guest_details(db: Session, user_id: str, checkout_id: str, *, full_name: str, email: str, line1: str,
                      city: str, state: str, postal_code: str, line2: Optional[str] = None) -> Checkout:
    """Guest checkout: who to ship to and where to send the confirmation. The
    address is kept for this order only; no customer account is created."""
    from backend.shop import profile
    co = get_checkout(db, user_id, checkout_id)
    if co.status != "open":
        raise CheckoutError("CHECKOUT_CLOSED", f"This checkout is {co.status} and can't be changed.", 409)
    if not is_guest_owner(db, user_id):
        raise CheckoutError("NOT_GUEST", "You're logged in, so this order uses your account details.", 400)
    email = (email or "").strip().lower()
    if not _EMAIL.match(email) or len(email) > 255:
        raise CheckoutError("INVALID_EMAIL", "Please enter a valid email address, like you@example.com.")
    try:
        address = profile.add_address(db, user_id, full_name=full_name, line1=line1, line2=line2, city=city,
                                      state=state, postal_code=postal_code, label="Shipping", make_default=True)
    except profile.ProfileError as exc:
        raise CheckoutError(exc.code, exc.message, exc.status)
    co.address_id, co.guest_email, co.guest_name = address.address_id, email, address.full_name
    _recalculate(db, co)
    db.commit()
    return co


def cancel_checkout(db: Session, user_id: str, checkout_id: str) -> Checkout:
    co = get_checkout(db, user_id, checkout_id)
    if co.status in ("open", "consented", "awaiting_payment"):   # (a pending provider session then can't complete)
        co.status = "cancelled"
        _invalidate_instructions(db, co)
        db.commit()
    return co


def refresh(db: Session, co: Checkout) -> list[dict]:
    """Re-check prices and stock right before payment (no commit)."""
    return _recalculate(db, co)


def invoice(db: Session, co: Checkout, lines: list[dict], path: dict, on_wallet: bool, wallet_view: Optional[dict],
            card: Optional[PaymentMethod]) -> dict:
    """The merchant's authoritative final invoice (CH-4): what "Approve & Pay" approves.
    Every number is ShopSphere's; nothing here comes from the browser or the LLM."""
    from backend.vic.common import MERCHANT_ID, MERCHANT_NAME
    discounts = []
    offer = db.query(offers.MemberOffer).filter_by(offer_id=co.offer_id).first() if co.offer_id else None
    if offer and co.offer_discount:
        discounts.append({"label": f"Chase card offer · {offer.title}", "amount": round(co.offer_discount, 2)})
    if co.points_discount:
        discounts.append({"label": f"{co.points_used:,} points", "amount": round(co.points_discount, 2)})
    benefits = json.loads(co.benefits_json) if co.benefits_json else {"applied": [], "removed": []}
    paying = (wallet_view or {}).get("display") if on_wallet else (card_dict(card) if card else None)
    if co.pay_with == "provider":
        paying = None
    return {
        "merchant_id": MERCHANT_ID, "merchant_name": MERCHANT_NAME, "order_ref": co.checkout_id,
        "checkout_version": co.version or 1, "action": "PAY",
        "items": [{"name": ln["name"], "sku": ln["sku"], "quantity": ln["quantity"], "unit_price": ln["unit_price"],
                   "line_total": round(ln["unit_price"] * ln["quantity"], 2)} for ln in lines],
        "subtotal": co.subtotal, "discounts": discounts,
        "eligible_benefits": [b for b in benefits["applied"] if b["kind"] in ("bonus_points", "cashback")],
        "tax": co.tax, "shipping": co.shipping, "final_amount": co.total, "currency": co.currency,
        "payment": ({"type": "provider", "display": PROVIDER_VIEW["display"], "route": "provider_handoff",
                     "page_owner": "Simulated Secure Payment Provider"} if co.pay_with == "provider" else
                    {"type": "wallet", "display": paying} if on_wallet else
                    {"type": "card", "display": paying.get("label") or paying["display"], "issuer": paying["issuer"],
                     "network": paying["network"], "route": path.get("path")} if paying else None),
    }


def _hosted_url(session_id: str) -> str:
    from backend.gateway.adapter import get_adapter
    return get_adapter().hosted_url(session_id)


def snapshot(db: Session, co: Checkout, issues: Optional[list[dict]] = None) -> dict:
    """The full review-screen view of a checkout."""
    lines = _lines(co)
    if issues is None:
        issues = [{"code": "OUT_OF_STOCK", "sku": ln["sku"], "name": ln["name"],
                   "requested": ln["quantity"], "available": ln.get("stock", 0)}
                  for ln in lines if ln.get("stock", 0) < ln["quantity"]]
    address = db.query(Address).filter_by(address_id=co.address_id).first() if co.address_id else None
    card = db.query(PaymentMethod).filter_by(payment_method_id=co.payment_method_id).first() if co.payment_method_id else None
    guest = is_guest_owner(db, co.user_id)
    wallet = None if guest else db.query(Wallet).filter_by(user_id=co.user_id).first()
    on_wallet = (co.pay_with == "wallet") and wallet is not None
    on_provider = co.pay_with == "provider"
    problems = list(issues)
    if guest and not co.guest_email:
        problems.append({"code": "NO_GUEST_DETAILS", "message": "Add your name, email and shipping address."})
    if not address:
        problems.append({"code": "NO_ADDRESS", "message": "Add a shipping address."})
    if on_wallet:
        if wallet.balance < co.total:
            problems.append({"code": "INSUFFICIENT_WALLET", "message": "Your wallet balance doesn't cover this order."})
        from backend.shop import eligibility      # CH-2: checkout is for credit-card holders, wallet too
        if not eligibility.is_credit_card_holder(db, co.user_id):
            problems.append({"code": "CREDIT_CARD_REQUIRED", "message": eligibility.MESSAGES["CREDIT_CARD_REQUIRED"]})
    elif on_provider:
        pass                                       # CH-6: the card is given on the payment provider's own page
    elif not card:
        problems.append({"code": "NO_PAYMENT_METHOD", "message": "Choose a saved credit card, or pay with a new card "
                                                                 "through the secure payment provider."})
    wallet_view = ({"payment_method_id": "wallet", "type": "wallet", "display": "ShopSphere Wallet",
                    "balance": round(wallet.balance, 2), "enough": wallet.balance >= co.total} if wallet else None)
    std_days = max((ln.get("delivery_days") or 5) for ln in lines)
    dates = delivery_dates(std_days)
    offer = db.query(offers.MemberOffer).filter_by(offer_id=co.offer_id).first() if co.offer_id else None
    from backend.shop import vic                       # which payment path this card takes (Part 2)
    from backend.shop.entitlements import payment_for_checkout
    path = vic.payment_path(db, co)
    paying = payment_for_checkout(db, co, path["path"])   # CH-3: Chase benefits follow the selected card
    bonus = [] if guest else offers.bonus_points(db, co.user_id, lines, paying)
    cash = [] if guest or on_wallet else offers.cashback(db, co.user_id, lines, paying)
    return {
        "checkout_id": co.checkout_id,
        "status": co.status,
        "lines": [{k: v for k, v in ln.items() if k != "stock"} | {"in_stock": ln.get("stock", 0) >= ln["quantity"]}
                  for ln in lines],
        "item_count": sum(ln["quantity"] for ln in lines),
        "subtotal": co.subtotal, "tax": co.tax, "tax_rate": TAX_RATE, "shipping": co.shipping,
        "total": co.total, "currency": co.currency,
        "delivery": {
            "method": co.delivery_method,
            "date": co.delivery_date.date().isoformat() if co.delivery_date else None,
            "options": [
                {"method": "standard", "label": "Standard", "price": 0.0, "date": dates["standard"].isoformat()},
                {"method": "express", "label": "Express", "price": EXPRESS_PRICE, "date": dates["express"].isoformat()},
            ],
        },
        "address": address_dict(address) if address else None,
        "payment_method": (wallet_view if on_wallet else PROVIDER_VIEW if on_provider
                           else (card_dict(card) if card else None)),
        "pay_with": "wallet" if on_wallet else "provider" if on_provider else "card",
        # CH-6: a new card through the Simulated Secure Payment Provider (customers only)
        "provider_option": None if guest else {"available": True, "label": PROVIDER_VIEW["display"]},
        "provider_session": ({"payment_session_id": co.provider_session_id, "status": co.status,
                              "hosted_url": _hosted_url(co.provider_session_id)} if co.provider_session_id else None),
        "wallet": wallet_view,
        "guest": {"email": co.guest_email, "name": co.guest_name} if guest else None,
        "points_discount": co.points_discount or 0.0,
        # Phase 12: customers see their balance, what they can use, and what this order earns.
        "points": None if guest else {
            "balance": points_balance(db, co.user_id), "used": co.points_used or 0,
            "discount": co.points_discount or 0.0,
            "max_usable": max_points(db, co.user_id, round(co.subtotal - (co.offer_discount or 0.0), 2)),
            "value_per_point": POINT_VALUE,
        },
        "earn_points": int(co.total) + sum(b["points"] for b in bonus),   # base 1 point per $1 + bonus offers
        "base_points": int(co.total),
        # Part 2: member offer, bonus points and simulated cashback — all computed here, never sent by a client
        "offer": {"offer_id": offer.offer_id, "title": offer.title, "kind": offer.kind,
                  "discount": co.offer_discount or 0.0} if offer else None,
        "offer_discount": co.offer_discount or 0.0,
        "bonus_points": bonus,
        "cashback": cash,
        "payment_path": path,
        # CH-4: the version the customer approves, the Chase benefits applied / removed (with reasons),
        # and the authoritative invoice (everything the consent is scoped to)
        "checkout_version": co.version or 1,
        "benefits": json.loads(co.benefits_json) if co.benefits_json else {"applied": [], "removed": []},
        "invoice": invoice(db, co, lines, path, on_wallet, wallet_view, None if on_provider else card),
        "saved_addresses": [] if guest else [address_dict(a) for a in list_addresses(db, co.user_id)],
        "saved_payment_methods": [] if guest else [card_dict(c) for c in list_cards(db, co.user_id)],
        "issues": problems,
        "ready": not problems,
        "checkout_hash": co.checkout_hash,
    }
