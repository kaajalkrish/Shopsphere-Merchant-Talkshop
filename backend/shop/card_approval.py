"""
Cardholder payment approval: the simulated passkey step for EVERY saved card.

The Visa agent-payment path (VIC-inspired) has always asked the member to confirm with a
simulated passkey. This gives the same step to every other saved card: Mastercard, Amex,
Discover and a Visa without agent payments. It is network-neutral and is NOT part of any
card network's protocol: ShopSphere itself asks its signed-in cardholder to confirm this
exact order before the saved card token is used.

  start    a single-use challenge bound to the member, the checkout, its version and total
  verify   the member's tap (simulated); the challenge is checked once
  consume  confirm() claims a verified approval once. A changed order, an expired or used
           approval, or one for another order is refused, and nothing is charged.

Not WebAuthn, not 3-D Secure, not an issuer or network API: the card issuer isn't contacted
at this step. The nonce goes to the browser only, never into Talkshop events or the LLM.
"""
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from backend.db.schema import Checkout, PasskeyChallenge
from backend.shop import checkout as checkout_service
from backend.shop import payment_routing
from backend.vic import passkey
from backend.vic.common import VicError, now

PURPOSE = "card_payment"
APPROVAL_TTL = timedelta(minutes=5)        # from the passkey tap to Approve & Pay going through
LABEL = "Simulated passkey authentication (demo, not a real passkey; your card issuer isn't contacted)"

CheckoutError = checkout_service.CheckoutError


def needed(path: dict) -> bool:
    """Saved cards on the standard route (any network). The Visa agent path has its own
    passkey-authenticated instruction; a new card verifies on the provider's page."""
    return path.get("route") == "saved_card"


def _subject(co: Checkout) -> str:
    return f"{co.checkout_id}:v{co.version or 1}:{round(co.total * 100)}"


def start(db: Session, user_id: str, checkout_id: str) -> dict:
    co = checkout_service.get_checkout(db, user_id, checkout_id)
    if co.status != "open":
        raise CheckoutError("CHECKOUT_CLOSED", f"This checkout is {co.status}.", 409)
    path = payment_routing.route(db, co)
    if not needed(path):
        raise CheckoutError("APPROVAL_NOT_NEEDED", "This way of paying has its own approval step.", 409)
    ch, nonce = passkey.create(db, user_id, PURPOSE, _subject(co))
    db.commit()
    card = path.get("card") or {}
    return {"approval": {"approval_id": ch.challenge_id, "checkout_id": co.checkout_id,
                         "checkout_version": co.version or 1, "amount": co.total, "currency": co.currency or "USD",
                         "card": card.get("display"), "network": path.get("network")},
            "passkey": {**passkey.public(ch, nonce), "label": LABEL}}


def verify(db: Session, user_id: str, checkout_id: str, approval_id: str, nonce: str, user_verified: bool) -> dict:
    co = checkout_service.get_checkout(db, user_id, checkout_id)
    _same_order(db, user_id, co, approval_id)
    try:
        passkey.verify(db, user_id, approval_id, nonce, user_verified, PURPOSE, _subject(co))
    except VicError as exc:
        db.commit()                                 # keep the failed / expired result
        raise CheckoutError(exc.code, exc.message, exc.status)
    db.commit()
    return {"approval_id": approval_id, "status": "verified", "checkout_version": co.version or 1}


def _same_order(db: Session, user_id: str, co: Checkout, approval_id: str) -> PasskeyChallenge:
    ch = db.query(PasskeyChallenge).filter_by(challenge_id=approval_id, user_id=user_id, purpose=PURPOSE).first()
    if not ch or not ch.subject_ref.startswith(f"{co.checkout_id}:"):
        raise CheckoutError("UNKNOWN_APPROVAL", "That approval doesn't belong to this order.", 404)
    if ch.subject_ref != _subject(co):
        raise CheckoutError("APPROVAL_STALE", "Your order changed after you approved it, so nothing was charged. "
                            f"Please approve the new total of ${co.total:.2f}.", 409,
                            {"checkout": checkout_service.snapshot(db, co)})
    return ch


def consume(db: Session, user_id: str, co: Checkout, approval_id: str) -> None:
    """Claim the member's verified approval for this exact order, once."""
    ch = _same_order(db, user_id, co, approval_id)
    if ch.result == "consumed":
        raise CheckoutError("APPROVAL_USED", "That approval was already used. Please approve the order again.", 409)
    if ch.result != "verified" or not ch.used_at:
        raise CheckoutError("APPROVAL_NOT_VERIFIED", "Please confirm this payment with your passkey first.", 409)
    if ch.used_at + APPROVAL_TTL < now():
        raise CheckoutError("APPROVAL_EXPIRED", "Your approval expired, so nothing was charged. Please approve the "
                            "order again.", 410)
    claimed = db.execute(update(PasskeyChallenge)
                         .where(PasskeyChallenge.id == ch.id, PasskeyChallenge.result == "verified")
                         .values(result="consumed")).rowcount
    if not claimed:
        raise CheckoutError("APPROVAL_USED", "That approval was already used. Please approve the order again.", 409)
