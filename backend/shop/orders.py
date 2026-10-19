"""
Order service — finalizes a purchase only after payment is authorized:
public order id (SS-#####), order lines, address and masked-card snapshots,
delivery date, stock committed, purchased lines removed from the cart.
"""
import hmac
import json
import random
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from backend.db.schema import (Address, CartItem, Checkout, MemberOffer, Order, OrderLine, PaymentMethod, Product,
                               ProductVariant, User)
from backend.shop import offers, rewards, shipping
from backend.shop.profile import address_dict


def new_display_id(db: Session) -> str:
    while True:
        candidate = f"SS-{random.randint(10000, 99999)}"
        if not db.query(Order).filter_by(display_id=candidate).first():
            return candidate


def finalize(db: Session, co: Checkout, payment_ref: str, payment_path: Optional[str] = None,
             provider_card: Optional[dict] = None) -> Order:
    """Turn the authorized payment's order row into the full ShopSphere order.
    Runs once: an order that is already finalized is returned unchanged, so a
    retry can never commit stock, points or offers twice."""
    order = db.query(Order).filter_by(order_id=payment_ref).one()
    if order.display_id:
        return order
    lines = json.loads(co.lines_json)
    address = db.query(Address).filter_by(address_id=co.address_id).first()
    card = db.query(PaymentMethod).filter_by(payment_method_id=co.payment_method_id).first()

    order.display_id = new_display_id(db)
    order.checkout_id = co.checkout_id
    order.subtotal, order.tax, order.shipping = co.subtotal, co.tax, co.shipping
    order.amount = co.total
    order.delivery_method, order.delivery_date = co.delivery_method, co.delivery_date
    order.ship_to_json = json.dumps(address_dict(address)) if address else None
    order.payment_method_type = co.pay_with or "card"
    if order.payment_method_type == "wallet":
        order.payment_brand, order.payment_last4 = "ShopSphere Wallet", None
    else:
        # CH-6: a new card through the provider: only its network + last 4 are known to ShopSphere
        src = provider_card or ({"brand": card.brand, "last4": card.last4} if card else None)
        order.payment_brand, order.payment_last4 = (src["brand"], src["last4"]) if src else (None, None)
    order.payment_status = "authorized"
    # Guest order: the checkout email/name, never a customer account (Phase 10)
    order.guest_email, order.guest_name = co.guest_email, co.guest_name
    shipping.start(order)
    # Loyalty points (Phase 12): spent only now that the payment is authorized;
    # earned at 1 point per $1 paid (the payment step credits them; customers only).
    owner = db.query(User).filter_by(user_id=co.user_id).first()
    is_customer = bool(owner and not owner.is_guest)
    order.points_used, order.points_discount = co.points_used or 0, co.points_discount or 0.0
    order.points_earned = int(order.amount) if is_customer else 0
    if is_customer and order.points_used:
        rewards.redeem(db, co.user_id, order.order_id, order.points_used)
    # Part 2: member offer, bonus points and simulated cashback (once per order)
    order.payment_path = payment_path or ("wallet" if order.payment_method_type == "wallet" else "standard")
    order.offer_id, order.offer_discount = co.offer_id, co.offer_discount or 0.0
    if co.offer_id and co.offer_discount:
        offer = db.query(MemberOffer).filter_by(offer_id=co.offer_id).first()
        offers.record(db, co.user_id, order.order_id, co.offer_id, offer.kind if offer else "discount",
                      co.offer_discount)
    order.bonus_points, order.cashback = 0, 0.0
    if is_customer:
        # CH-3: bonus points and cashback follow the card the order was paid with
        from backend.shop.entitlements import payment_for_checkout
        paying = payment_for_checkout(db, co, order.payment_path)
        for b in offers.bonus_points(db, co.user_id, lines, paying):
            if offers.record(db, co.user_id, order.order_id, b["offer_id"], "bonus_points", b["points"]):
                rewards.earn(db, co.user_id, order.order_id, b["points"], f"Bonus: {b['title']}", key=f"BONUS-{b['offer_id']}")
                order.bonus_points += b["points"]
        for c in offers.cashback(db, co.user_id, lines, paying):
            if offers.record(db, co.user_id, order.order_id, c["offer_id"], "cashback", c["amount"]):
                order.cashback = round(order.cashback + c["amount"], 2)
        order.points_earned += order.bonus_points

    for ln in lines:
        db.add(OrderLine(
            order_id=order.order_id, sku=ln["sku"], product_id=ln["product_id"], product_name=ln["name"],
            size=ln.get("size"), color=ln.get("color"), quantity=ln["quantity"],
            unit_price=ln["unit_price"], line_total=round(ln["unit_price"] * ln["quantity"], 2),
            image_url=ln.get("image_url"),
        ))
        # Commit stock: guarded decrement so a concurrent sale can't push it negative.
        db.execute(update(ProductVariant)
                   .where(ProductVariant.sku == ln["sku"], ProductVariant.stock >= ln["quantity"])
                   .values(stock=ProductVariant.stock - ln["quantity"]))
        product = db.query(Product).filter_by(product_id=ln["product_id"]).first()
        if product:
            product.inventory = max(0, (product.inventory or 0) - ln["quantity"])

    # Take what was bought out of the cart: the line goes if all of it was
    # bought, otherwise it keeps the remainder (e.g. one of two pairs).
    for ln in lines:
        item = db.query(CartItem).filter_by(cart_item_id=ln.get("line_id"), user_id=co.user_id).first()
        if item:
            if item.quantity > ln["quantity"]:
                item.quantity -= ln["quantity"]
            else:
                db.delete(item)
    co.status = "paid"
    db.commit()
    return order


def order_dict(db: Session, order: Order) -> dict:
    lines = db.query(OrderLine).filter_by(order_id=order.order_id).order_by(OrderLine.id).all()
    return {
        "order_id": order.display_id or order.order_id,
        "internal_order_id": order.order_id,
        "status": "confirmed" if order.status == "confirmed" else order.status,
        "payment_status": order.payment_status,
        "lines": [{"sku": ln.sku, "product_id": ln.product_id, "name": ln.product_name, "size": ln.size,
                   "color": ln.color, "quantity": ln.quantity, "unit_price": ln.unit_price,
                   "line_total": ln.line_total, "image_url": ln.image_url} for ln in lines],
        "subtotal": order.subtotal, "tax": order.tax, "shipping": order.shipping, "total": order.amount,
        "currency": order.currency,
        "delivery_method": order.delivery_method,
        "delivery_date": order.delivery_date.date().isoformat() if order.delivery_date else None,
        "ship_to": json.loads(order.ship_to_json) if order.ship_to_json else None,
        "payment": _payment_view(order),
        "payment_method_type": order.payment_method_type or "card",
        "guest": bool(order.guest_email),
        "guest_email": order.guest_email,
        "shipment": shipping.view(order),
        "points": {"used": order.points_used or 0, "discount": order.points_discount or 0.0,
                   "earned": order.points_earned or 0, "bonus": order.bonus_points or 0},
        "offer": _offer_view(db, order),
        "cashback": order.cashback or 0.0,
        "payment_path": order.payment_path,
        "tracking_number": order.tracking_number,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


def _offer_view(db: Session, order: Order) -> Optional[dict]:
    if not order.offer_id or not order.offer_discount:
        return None
    o = db.query(MemberOffer).filter_by(offer_id=order.offer_id).first()
    return {"offer_id": order.offer_id, "title": o.title if o else order.offer_id, "discount": order.offer_discount}


def _payment_view(order: Order) -> Optional[dict]:
    if order.payment_method_type == "wallet":
        return {"brand": "ShopSphere Wallet", "last4": None, "display": "ShopSphere Wallet"}
    if order.payment_last4:
        return {"brand": order.payment_brand, "last4": order.payment_last4,
                "display": f"{order.payment_brand} •••• {order.payment_last4}"}
    return None


NOT_FOUND = "We could not find an order matching the provided Order ID and email address."


def find_for_tracking(db: Session, order_id: str, email: str) -> Optional[Order]:
    """Order ID + the email used for it (guest checkout email, or the customer's
    account email). Any mismatch looks the same as a missing order."""
    order = db.query(Order).filter(Order.display_id == (order_id or "").strip().upper()).first()
    given = (email or "").strip().lower()
    expected = ""
    if order is not None:
        if order.guest_email:
            expected = order.guest_email.lower()
        else:
            owner = db.query(User).filter_by(user_id=order.user_id).first()
            expected = (owner.email or "").lower() if owner and not owner.is_guest else ""
    ok = hmac.compare_digest(given.encode(), expected.encode()) if expected else False
    return order if (order is not None and given and ok) else None


def tracking_view(db: Session, order: Order) -> dict:
    """What Track Order shows: no full address, no card details, no internal ids."""
    d = order_dict(db, order)
    ship_to = d["ship_to"] or {}
    return {
        "order_id": d["order_id"], "status": d["status"], "payment_status": d["payment_status"],
        "lines": [{k: ln[k] for k in ("name", "size", "color", "quantity", "line_total", "image_url")}
                  for ln in d["lines"]],
        "subtotal": d["subtotal"], "tax": d["tax"], "shipping": d["shipping"], "total": d["total"],
        "delivery_method": d["delivery_method"], "delivery_date": d["delivery_date"],
        "ship_to": {"city": ship_to.get("city"), "state": ship_to.get("state")},
        "payment": d["payment"], "guest": d["guest"], "shipment": d["shipment"],
        "created_at": d["created_at"],
    }


def find_order(db: Session, user_id: str, order_id: str) -> Optional[Order]:
    """Look up by public id (SS-48291) or internal id."""
    return (db.query(Order).filter(Order.user_id == user_id)
            .filter((Order.display_id == order_id) | (Order.order_id == order_id)).first())
