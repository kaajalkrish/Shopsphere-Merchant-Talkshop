"""
Guest checkout for the chat-app connector: buy without an account or a sign-in.

Built only from the shared checkout services (backend/shop/checkout.py): the same live prices,
stock checks, delivery dates, tax, versioning and stale-total detection as every other checkout.
What makes it a guest checkout:

  owner      the chat's anonymous visitor (users.is_guest=True): no password, no login, no
             saved cards, no wallet, no points, no Chase entitlements (is_guest_owner() checks)
  access     a signed, 2-hour guest token (backend/mcp/auth.py) naming this visitor + checkout;
             a checkout id alone reaches nothing
  details    name, email and shipping address via set_guest_details(): kept on this order only
  payment    a new card on the Simulated Secure Payment Provider's page only, never saved
             (payment.confirm(..., guest=True))
  order      a guest order (orders.guest_email), confirmation email to the guest's address,
             tracked later with Order ID + that email
"""
from typing import Optional

from sqlalchemy.orm import Session

from backend.auth.dependencies import CurrentUser
from backend.db.schema import CartItem, User
from backend.mcp import auth as mcp_auth
from backend.shop import checkout as checkout_service
from backend.shop.checkout import CheckoutError

EXPIRED = "This guest checkout has expired or isn't yours. Start checkout again from your cart."


def visitor(db: Session, visitor_id: str) -> CurrentUser:
    u = db.query(User).filter_by(user_id=visitor_id).first()
    if not u or not u.is_guest:
        raise CheckoutError("GUEST_SESSION_INVALID", EXPIRED, 403)
    return CurrentUser(user_id=u.user_id, name=u.name or "Guest", email=u.email or "", is_visitor=True)


def start(db: Session, visitor_id: str, line_ids: Optional[list[str]] = None,
          delivery: Optional[str] = None) -> tuple[dict, str]:
    """A guest checkout for the visitor's cart (or the chosen lines): (snapshot, guest token)."""
    visitor(db, visitor_id)
    lines = db.query(CartItem).filter_by(user_id=visitor_id).all()
    ids = [it.cart_item_id for it in lines if not line_ids or it.cart_item_id in line_ids]
    if not ids:
        raise CheckoutError("NO_ITEMS", "The ShopSphere cart is empty. Add something first.")
    co = checkout_service.create_checkout(db, visitor_id, ids)
    co.pay_with = "provider"                       # a guest pays only with a new card on the secure page
    if delivery in checkout_service.DELIVERY_LABELS:
        co.delivery_method = delivery
    checkout_service.refresh(db, co)
    db.commit()
    return checkout_service.snapshot(db, co), mcp_auth.issue_guest_token(visitor_id, co.checkout_id)


def load(db: Session, token: Optional[str]) -> tuple[CurrentUser, str]:
    """The guest and their checkout id from a guest token. Refuses an expired or forged token,
    or a checkout that isn't this visitor's own guest checkout."""
    found = mcp_auth.read_guest_token(token)
    if not found:
        raise CheckoutError("GUEST_SESSION_INVALID", EXPIRED, 403)
    user = visitor(db, found[0])
    checkout_service.get_checkout(db, user.user_id, found[1])      # 404 unless it is the visitor's
    return user, found[1]


def update(db: Session, token: Optional[str], *, delivery: Optional[str] = None,
           quantities: Optional[dict[str, int]] = None, details: Optional[dict] = None) -> dict:
    """Change delivery / quantities and save the guest's contact + shipping details. All
    validation is the server's (email, address, ZIP, stock); amounts are recalculated here."""
    user, checkout_id = load(db, token)
    if details is not None:
        checkout_service.set_guest_details(db, user.user_id, checkout_id, **details)
    if delivery is not None or quantities:
        checkout_service.update_checkout(db, user.user_id, checkout_id, delivery_method=delivery,
                                         quantities=quantities or None)
    return checkout_service.snapshot(db, checkout_service.get_checkout(db, user.user_id, checkout_id))
