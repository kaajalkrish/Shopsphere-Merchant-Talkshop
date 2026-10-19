"""
Talkshop's tools — thin wrappers over ShopSphere's merchant services
(backend/shop). The LLM chooses *which* of LLM_TOOLS should run; this code
runs it and returns exactly what the service returned.

confirm_and_pay is deliberately NOT in LLM_TOOLS: the orchestrator calls it
only for the shopper's GO AHEAD action, never from model output.
"""
from typing import Optional

from sqlalchemy.orm import Session

from backend.auth.dependencies import CurrentUser
from backend.db.init_db import get_engine
from backend.shop import cart as cart_service
from backend.shop import catalog
from backend.shop import checkout as checkout_service
from backend.shop import payment as payment_service

# Shown to the LLM so it can pick an action (orchestrator enforces stage rules).
LLM_TOOLS = {
    "search": "Search the ShopSphere catalog for products matching the shopper's request.",
    "select_product": "The shopper picked one of the products shown (or named a product).",
    "choose_option": "The shopper answered the size/option or colour question.",
    "checkout": "The shopper wants to proceed to checkout with the item(s) Talkshop added.",
    "keep_shopping": "The shopper doesn't want to check out yet.",
    "update_checkout": "Change delivery (standard/express), quantity, address or card on the review screen.",
    "cancel_checkout": "The shopper doesn't want to place this order.",
    "track_order": "The shopper asks about an order they already placed (status, tracking, delivery, order details).",
    "answer": "Answer a question or comment without changing anything.",
}


def _db() -> Session:
    return Session(get_engine())


# Department words the shopper might use, mapped to catalog filters.
_DEPARTMENTS = {"shoes": "shoes", "clothing": "clothing", "electronics": "electronics", "accessories": "accessories"}


def search_products(*, query: str, category: Optional[str] = None, brand: Optional[str] = None,
                    max_price: Optional[float] = None, gender: Optional[str] = None,
                    color: Optional[str] = None, limit: int = 3) -> dict:
    """Returns {"products": [...summaries], "brand_missing": bool, "relaxed": [...]}."""
    filters = {}
    if category in _DEPARTMENTS:
        filters["department"] = _DEPARTMENTS[category]
    elif category and category not in ("general", "chitchat"):
        filters["category"] = category
    with _db() as db:
        brand_missing = False
        if brand and not catalog.search(db, brand=brand):
            brand_missing, brand = True, None              # "we don't carry X — here are similar ones"
        products = catalog.search(db, q=query, brand=brand, max_price=max_price, gender=gender,
                                  color=color, limit=limit, **filters)
        relaxed = []
        if not products and color:                        # relax one filter at a time, say which
            products = catalog.search(db, q=query, brand=brand, max_price=max_price, gender=gender, limit=limit, **filters)
            if products:
                relaxed.append("color")
        if not products and max_price is not None:
            products = catalog.search(db, q=query, brand=brand, gender=gender, limit=limit, **filters)
            if products:
                relaxed.append("max_price")
        return {"products": [catalog.product_summary(p) for p in products],
                "brand_missing": brand_missing, "relaxed": relaxed}


def get_product(product_id: str) -> Optional[dict]:
    with _db() as db:
        p = catalog.get_product(db, product_id)
        return catalog.product_detail(p) if p else None


def check_variant(product_id: str, size: Optional[str] = None, color: Optional[str] = None) -> dict:
    with _db() as db:
        return catalog.availability(catalog.get_product(db, product_id), size=size, color=color)


def add_to_cart(user_id: str, sku: str, quantity: int = 1) -> tuple[dict, str]:
    with _db() as db:
        return cart_service.add_line(db, user_id, sku, quantity)


def get_cart(user_id: str) -> dict:
    with _db() as db:
        return cart_service.get_cart(db, user_id)


def create_checkout(user_id: str, line_ids: list[str], quantities: Optional[dict[str, int]] = None) -> dict:
    with _db() as db:
        return checkout_service.snapshot(db, checkout_service.create_checkout(db, user_id, line_ids, quantities))


def get_checkout(user_id: str, checkout_id: str) -> dict:
    with _db() as db:
        return checkout_service.snapshot(db, checkout_service.get_checkout(db, user_id, checkout_id))


def update_checkout(user_id: str, checkout_id: str, **changes) -> dict:
    with _db() as db:
        return checkout_service.snapshot(db, checkout_service.update_checkout(db, user_id, checkout_id, **changes))


async def cancel_checkout(user: CurrentUser, checkout_id: str) -> dict:
    """CH-8: cancel before paying — recorded, and a pending provider session released."""
    return await payment_service.cancel(user, checkout_id)


def record_approval_cancelled(user_id: str, checkout_id: str, reason: str) -> None:
    payment_service.record_approval_cancelled(user_id, checkout_id, reason)


def track_order(order_id: str, email: str) -> Optional[dict]:
    """Order ID + the email used for it — the same check as the website's Track Order."""
    from backend.shop import orders as order_service
    with _db() as db:
        order = order_service.find_for_tracking(db, order_id, email)
        return order_service.tracking_view(db, order) if order else None


async def complete_provider(user: CurrentUser, checkout_id: str, session_id: str) -> dict:
    """CH-6: verify the payment provider's result server-to-server and place the order.
    Like confirm_and_pay, never offered to the LLM."""
    return await payment_service.complete_provider(user, checkout_id, session_id)


async def abandon_provider(user: CurrentUser, checkout_id: str, session_id: str) -> dict:
    """The shopper closed the secure payment sheet: release the session, back to review."""
    return await payment_service.abandon_provider(user, checkout_id, session_id)


def has_chase_benefits(user_id: str) -> bool:
    """CH-3: an eligible Chase credit card is linked (Chase offers, 2x points with it)."""
    from backend.shop.eligibility import has_chase_relationship
    with _db() as db:
        return has_chase_relationship(db, user_id)


def points_balance(user_id: str) -> int:
    with _db() as db:
        return checkout_service.points_balance(db, user_id)


def own_orders(user_id: str, limit: int = 10) -> list[dict]:
    """A logged-in customer's recent ShopSphere orders, newest first, each with
    the words that describe it (product names, brands, categories) for matching
    "my nike shoes" to the right order."""
    from backend.db.schema import Order, OrderLine, Product
    from backend.shop import orders as order_service
    with _db() as db:
        rows = (db.query(Order).filter(Order.user_id == user_id, Order.display_id.isnot(None))
                .order_by(Order.created_at.desc(), Order.id.desc()).limit(limit).all())
        out = []
        for order in rows:
            words = []
            for ln in db.query(OrderLine).filter_by(order_id=order.order_id).all():
                p = db.query(Product).filter_by(product_id=ln.product_id).first()
                words += [ln.product_name or "", ln.color or ""] + ([p.brand or "", p.category or "", p.department or ""] if p else [])
            out.append({"view": order_service.tracking_view(db, order),
                        "match": " ".join(words).lower().replace("_", " ")})
        return out


def own_order(user_id: str, order_id: str) -> Optional[dict]:
    """A logged-in customer's own order: no email needed."""
    from backend.shop import orders as order_service
    with _db() as db:
        order = order_service.find_order(db, user_id, (order_id or "").strip().upper())
        return order_service.tracking_view(db, order) if order and order.display_id else None


async def confirm_and_pay(user: CurrentUser, checkout_id: str, consent_mode: str = "button",
                          instruction_id: Optional[str] = None, scope: Optional[dict] = None,
                          return_url: Optional[str] = None) -> dict:
    """Only ever called for the member's Approve-and-pay action (tapped, or the
    review card's countdown on the standard path). On the Visa path it also
    needs the member's AUTHENTICATED instruction (simulated passkey, done by
    the panel directly with ShopSphere). Never by the LLM."""
    scope = scope or {}
    return await payment_service.confirm(user, checkout_id, consent=True, consent_mode=consent_mode,
                                         instruction_id=instruction_id, action="PAY",
                                         checkout_version=scope.get("checkout_version"),
                                         final_amount=scope.get("final_amount"), currency=scope.get("currency"),
                                         return_url=return_url)
