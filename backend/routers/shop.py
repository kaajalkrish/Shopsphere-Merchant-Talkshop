"""
ShopSphere merchant services API (Demo 1, Phase 2) — no AI in any endpoint.
The website calls these directly; Talkshop calls the same services as tools.

Cart (SKU-based; every call returns the full cart)
  GET    /api/cart/lines
  POST   /api/cart/lines                {sku, quantity}
  PATCH  /api/cart/lines/{line_id}      {quantity}   (0 removes)
  DELETE /api/cart/lines/{line_id}

Customer profile (cards are stored masked: an opaque token + network + last 4)
  GET/POST /api/me/addresses              DELETE /api/me/addresses/{id}
  GET /api/me/payment-methods             DELETE /api/me/payment-methods/{id}
  POST /api/me/payment-methods            410 PAN_NOT_ACCEPTED (CH-6: never takes card numbers)

Checkout + payment
  POST /api/checkouts                       {line_ids}       → review snapshot
  GET  /api/checkouts/{id}
  PATCH /api/checkouts/{id}                 {delivery_method?, address_id?, payment_method_id?, quantities?}
  POST /api/checkouts/{id}/cancel
  POST /api/checkouts/{id}/confirm          {consent: true, action: "PAY", checkout_version, final_amount, currency}
                                            → authorized order | declined | blocked | 409 STALE_CHECKOUT

The older /api/cart and /api/cart/items endpoints (product-level, no SKU) stay
for the current chat UI until Demo 1 Phase 5 replaces it.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth.dependencies import CurrentUser, get_current_user, require_customer
from backend.db.init_db import get_engine
from backend.shop import cart as cart_service
from backend.shop import checkout as checkout_service
from backend.shop import payment as payment_service
from backend.shop import notifications
from backend.shop import orders as order_service
from backend.shop import profile as profile_service
from backend.shop import shipping
from backend.shop.eligibility import require_shopper

# CH-2 access levels (backend/shop/eligibility.py): the cart is open to everyone
# (visitors too); checkout, saved details and payment need a signed-in customer
# (any card issuer); paying also needs a credit card (checked in payment.confirm).
# Order tracking by Order ID + email is public. The issuer is never an access rule.
router = APIRouter(tags=["shop"])


def get_db():
    with Session(get_engine()) as session:
        yield session


def eligibility_check(db: Session, user: CurrentUser) -> Optional[JSONResponse]:
    """Checkout-level access for a signed-in user (an inactive account is refused)."""
    from backend.shop import eligibility
    code = eligibility.check_access(db, user)
    if not code:
        return None
    return JSONResponse(status_code=403, content={"detail": {"code": code, "message": eligibility.MESSAGES[code]}})


def _error(exc) -> JSONResponse:
    body = {"code": exc.code, "message": exc.message}
    body.update(getattr(exc, "detail", None) or {})
    return JSONResponse(status_code=exc.status, content={"detail": body})


# ── Cart ─────────────────────────────────────────────────────────────────────

class AddLineRequest(BaseModel):
    sku: str
    quantity: int = Field(1, ge=1, le=20)


class QuantityRequest(BaseModel):
    quantity: int = Field(..., ge=0, le=20)


@router.get("/api/cart/lines")
def get_cart(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return cart_service.get_cart(db, user.user_id)


@router.post("/api/cart/lines")
def add_cart_line(req: AddLineRequest, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        cart, line_id = cart_service.add_line(db, user.user_id, req.sku, req.quantity)
    except cart_service.CartError as exc:
        return _error(exc)
    return {**cart, "added_line_id": line_id}


@router.patch("/api/cart/lines/{line_id}")
def update_cart_line(line_id: str, req: QuantityRequest, user: CurrentUser = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    try:
        return cart_service.set_quantity(db, user.user_id, line_id, req.quantity)
    except cart_service.CartError as exc:
        return _error(exc)


@router.delete("/api/cart/lines/{line_id}")
def delete_cart_line(line_id: str, user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        return cart_service.remove_line(db, user.user_id, line_id)
    except cart_service.CartError as exc:
        return _error(exc)


# ── Customer profile ─────────────────────────────────────────────────────────

class AddressRequest(BaseModel):
    full_name: str
    line1: str
    line2: Optional[str] = None
    city: str
    state: str
    postal_code: str
    label: Optional[str] = None
    make_default: bool = False


@router.get("/api/me/addresses")
def get_addresses(user: CurrentUser = Depends(require_customer), db: Session = Depends(get_db)):
    return [profile_service.address_dict(a) for a in profile_service.list_addresses(db, user.user_id)]


@router.post("/api/me/addresses")
def add_address(req: AddressRequest, user: CurrentUser = Depends(require_customer), db: Session = Depends(get_db)):
    try:
        a = profile_service.add_address(db, user.user_id, **req.model_dump())
    except profile_service.ProfileError as exc:
        return _error(exc)
    return profile_service.address_dict(a)


@router.get("/api/me/payment-methods")
def get_cards(user: CurrentUser = Depends(require_customer), db: Session = Depends(get_db)):
    return [profile_service.card_dict(c) for c in profile_service.list_cards(db, user.user_id)]


@router.post("/api/me/payment-methods")
def add_card(user: CurrentUser = Depends(require_shopper)):
    """CH-6: ShopSphere never accepts a card number or CVC — whatever is sent here is
    refused unread. A new card is given on the (simulated) payment provider's own page
    at checkout, and kept only if the customer ticks "save" there."""
    return JSONResponse(status_code=410, content={"detail": {
        "code": "PAN_NOT_ACCEPTED",
        "message": "For your security ShopSphere never takes card numbers. Pay with a new card through the secure "
                   "payment provider at checkout, and choose \"save\" there to keep it for next time."}})


@router.delete("/api/me/addresses/{address_id}")
def delete_address(address_id: str, user: CurrentUser = Depends(require_customer), db: Session = Depends(get_db)):
    try:
        default = profile_service.delete_address(db, user.user_id, address_id)
    except profile_service.ProfileError as exc:
        return _error(exc)
    checkout_service.reassign(db, user.user_id, "address_id", address_id, default)
    return {"deleted": address_id, "default_address_id": default}


@router.delete("/api/me/payment-methods/{payment_method_id}")
def delete_card(payment_method_id: str, user: CurrentUser = Depends(require_customer), db: Session = Depends(get_db)):
    try:
        default = profile_service.delete_card(db, user.user_id, payment_method_id)
    except profile_service.ProfileError as exc:
        return _error(exc)
    checkout_service.reassign(db, user.user_id, "payment_method_id", payment_method_id, default)
    return {"deleted": payment_method_id, "default_payment_method_id": default}


# ── Checkout + payment ───────────────────────────────────────────────────────

class CreateCheckoutRequest(BaseModel):
    line_ids: list[str] = Field(..., min_length=1)
    guest: bool = False             # Phase 10: a visitor checking out as a guest


class GuestDetailsRequest(BaseModel):
    full_name: str
    email: str
    line1: str
    line2: Optional[str] = None
    city: str
    state: str
    postal_code: str


class TrackRequest(BaseModel):
    order_id: str
    email: str


class UpdateCheckoutRequest(BaseModel):
    delivery_method: Optional[str] = None
    address_id: Optional[str] = None
    payment_method_id: Optional[str] = None
    pay_with: Optional[str] = None                # card | wallet (Phase 10)
    points_used: Optional[int] = None             # loyalty points to spend (Phase 12)
    quantities: Optional[dict[str, int]] = None   # cart line id → quantity


class ConfirmRequest(BaseModel):
    consent: bool = False   # true only from the member's explicit Approve and pay / Place order
    # Visa agent-payment path (Part 2): the member's AUTHENTICATED payment instruction
    instruction_id: Optional[str] = Field(None, max_length=80)
    consent_mode: str = Field("button", pattern="^(button|typed)$")
    # CH-4 consent scope: what the customer approved on the final invoice (compared, never trusted for the amount)
    action: Optional[str] = Field(None, pattern="^PAY$")
    checkout_version: Optional[int] = Field(None, ge=1)
    final_amount: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = Field(None, max_length=3)
    # CH-6: where the payment provider's page sends the customer back (ShopSphere's own origin only)
    return_url: Optional[str] = Field(None, max_length=300)


@router.post("/api/checkouts")
def create_checkout(req: CreateCheckoutRequest, user: CurrentUser = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    if user.is_visitor:                            # visitors sign in or sign up at checkout (no guest checkout)
        raise HTTPException(status_code=403, detail={
            "code": "LOGIN_REQUIRED", "message": "Sign in or create a ShopSphere account to check out. "
                                                 "Your cart comes with you."})
    require = eligibility_check(db, user)
    if require:
        return require
    try:
        co = checkout_service.create_checkout(db, user.user_id, req.line_ids)
    except checkout_service.CheckoutError as exc:
        return _error(exc)
    return checkout_service.snapshot(db, co)


@router.get("/api/checkouts/{checkout_id}")
def get_checkout(checkout_id: str, user: CurrentUser = Depends(require_shopper), db: Session = Depends(get_db)):
    try:
        return checkout_service.snapshot(db, checkout_service.get_checkout(db, user.user_id, checkout_id))
    except checkout_service.CheckoutError as exc:
        return _error(exc)


@router.patch("/api/checkouts/{checkout_id}")
def update_checkout(checkout_id: str, req: UpdateCheckoutRequest, user: CurrentUser = Depends(require_shopper),
                    db: Session = Depends(get_db)):
    try:
        co = checkout_service.update_checkout(db, user.user_id, checkout_id, **req.model_dump())
    except checkout_service.CheckoutError as exc:
        return _error(exc)
    return checkout_service.snapshot(db, co)


@router.post("/api/checkouts/{checkout_id}/cancel")
async def cancel_checkout(checkout_id: str, user: CurrentUser = Depends(require_shopper)):
    """Cancel before paying (CH-8: recorded as APPROVAL_CANCELLED; a pending provider
    session is released). Nothing is ever charged."""
    try:
        return await payment_service.cancel(user, checkout_id)
    except checkout_service.CheckoutError as exc:
        return _error(exc)


@router.post("/api/checkouts/{checkout_id}/confirm")
async def confirm_checkout(checkout_id: str, req: ConfirmRequest, user: CurrentUser = Depends(require_shopper)):
    """Signed-in customers who hold a credit card (checked in payment.confirm)."""
    try:
        return await payment_service.confirm(user, checkout_id, req.consent, consent_mode=req.consent_mode,
                                             instruction_id=req.instruction_id, action=req.action,
                                             checkout_version=req.checkout_version, final_amount=req.final_amount,
                                             currency=req.currency, return_url=req.return_url)
    except checkout_service.CheckoutError as exc:
        return _error(exc)


@router.post("/api/checkouts/{checkout_id}/payment-session/{session_id}/complete")
async def complete_payment_session(checkout_id: str, session_id: str, user: CurrentUser = Depends(require_shopper)):
    """CH-6: back from the payment provider's page. ShopSphere fetches the SAFE result from
    the provider itself (never from the browser), checks it is for this exact approved
    order, then places the order once. Repeat calls return the same order."""
    try:
        return await payment_service.complete_provider(user, checkout_id, session_id[:80])
    except checkout_service.CheckoutError as exc:
        return _error(exc)


@router.post("/api/checkouts/{checkout_id}/payment-session/{session_id}/abandon")
async def abandon_payment_session(checkout_id: str, session_id: str, user: CurrentUser = Depends(require_shopper)):
    """The customer closed the secure payment sheet: release the provider session and go back
    to review. Nothing is charged; a finished payment is not touched."""
    try:
        return await payment_service.abandon_provider(user, checkout_id, session_id[:80])
    except checkout_service.CheckoutError as exc:
        return _error(exc)


@router.post("/api/checkouts/{checkout_id}/guest-details")
def guest_details(checkout_id: str, req: GuestDetailsRequest, user: CurrentUser = Depends(require_shopper),
                  db: Session = Depends(get_db)):
    """Guest checkout: name, email and shipping address, straight to ShopSphere."""
    try:
        co = checkout_service.set_guest_details(db, user.user_id, checkout_id, **req.model_dump())
    except checkout_service.CheckoutError as exc:
        return _error(exc)
    return checkout_service.snapshot(db, co)


# ── Track Order (Phase 10): Order ID + email, for guests and customers ─────────

def _tracked(db: Session, req: TrackRequest):
    order = order_service.find_for_tracking(db, req.order_id, req.email)
    if order is None:
        return None, JSONResponse(status_code=404, content={"detail": {
            "code": "ORDER_NOT_FOUND", "message": order_service.NOT_FOUND}})
    return order, None


@router.post("/api/orders/track")
def track_order(req: TrackRequest, db: Session = Depends(get_db)):
    order, err = _tracked(db, req)
    return err or order_service.tracking_view(db, order)


@router.post("/api/orders/track/email")
def track_order_email(req: TrackRequest, db: Session = Depends(get_db)):
    """The order's confirmation email from the demo outbox (same Order ID + email check)."""
    order, err = _tracked(db, req)
    if err:
        return err
    return notifications.confirmation_for(db, order) or JSONResponse(status_code=404, content={"detail": {
        "code": "NO_EMAIL", "message": "No confirmation email was sent for this order."}})


@router.post("/api/orders/track/advance")
def track_order_advance(req: TrackRequest, db: Session = Depends(get_db)):
    """Demo control: move the simulated shipment to its next step."""
    order, err = _tracked(db, req)
    if err:
        return err
    shipping.advance(db, order)
    return order_service.tracking_view(db, order)
