"""
Purchase consent for agent-placed orders (MCP review_order → place_order).

A model calling a payment tool is never, by itself, a customer's authorization. ShopSphere keeps
every authorization request server-side (PurchaseConsent) and checks four things before paying:

  1. scope      one exact order: owner, merchant, checkout, version, total (cents), currency,
                order fingerprint and the opaque payment reference. Any change → refused; review again.
  2. freshness  15 minutes, and single use: consumed atomically just before payment. A newer
                review_order supersedes an older request for the same checkout. Declined → dead.
  3. the reply  the shopper's own words, as typed (customer_reply), must be a clear yes. "maybe",
                "no", a question or "yes but…" refuse. This only guards against misreading the chat;
                the model relays it, so it is NOT proof of consent on its own.
  4. the level  what actually proves consent depends on the payment route:
       sandbox_chat      a SANDBOX token from the fictional demo profile: the chat yes, plus Claude's own
                         permission prompt (place_order is marked destructive, so Claude asks the user
                         before running it), plus this single-use authorization. Simulated money only.
       customer_step_up  a real-world-style credential (a signed-in customer's card on file, the
                         Wallet, Visa agent payments, an external provider's reference): the customer
                         approves on ShopSphere's approval page in their own browser — a step the
                         model can't take. The passkey there is SIMULATED (not WebAuthn, no issuer).
       provider_page     a new card: verified by the customer on the (simulated) payment provider's
                         page, never in the chat.

The signed token the assistant carries (backend/mcp/auth.py, typ purchase_auth) only names a
consent row (jti) and repeats its terms; it is not an issuer authentication and isn't shown as one.
"""
import json
import re
import secrets
import time
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from backend.db.schema import PaymentMethod, PurchaseConsent
from backend.shop import guest_wallet

MERCHANT = "shopsphere"
TTL = 15 * 60
LEVEL_TEXT = {
    "sandbox_chat": "Sandbox card: your yes in the chat + Claude's permission prompt + a single-use authorization",
    "customer_step_up": "Your approval on ShopSphere's approval page (simulated passkey) + a single-use authorization",
    "provider_page": "Card entered and verified on the secure payment provider's page (simulated provider)",
}


class ConsentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ── The shopper's reply ────────────────────────────────────────────────────────

_UNCLEAR = re.compile(r"\b(maybe|perhaps|not sure|unsure|i guess|hmm+|later|let me think|think about|possibly|"
                      r"might|don'?t know|dunno|but|instead|change|wait a)\b")
_NO = re.compile(r"\b(no|not|nope|nah|don'?t|do not|stop|cancel|hold on|not yet|not now|never ?mind|decline|wait)\b")
_YES = re.compile(r"\b(yes|yeah|yep|yup|sure|ok|okay|proceed|go ahead|confirm(ed)?|authori[sz]e[ds]?|approved?|"
                  r"place (it|the order|my order|this order)|do it|buy it|sounds good|please do|let'?s do it|"
                  r"absolutely|of course|correct|go for it)\b")


def classify_reply(text: Optional[str]) -> str:
    """"yes", "no" or "unclear" for the shopper's verbatim answer to "Do you authorize…?"."""
    t = (text or "").strip().lower()
    if not t or len(t) > 300:
        return "unclear"
    if "?" in t or _UNCLEAR.search(t):
        return "unclear"
    if _NO.search(t):
        return "no"
    return "yes" if _YES.search(t) else "unclear"


# ── Which proof this payment route needs ───────────────────────────────────────

def level_for(db: Session, owner_id: str, pay_ref: str, guest: bool) -> str:
    if pay_ref == "provider":
        return "provider_page"
    if guest:
        pm = db.query(PaymentMethod).filter_by(payment_method_id=pay_ref, user_id=owner_id).first()
        if pm is not None and pm.source == guest_wallet.SOURCE:
            return "sandbox_chat"
    return "customer_step_up"


def pay_ref_of(snap: dict) -> str:
    if snap.get("pay_with") != "card":
        return str(snap.get("pay_with") or "none")
    return str((snap.get("payment_method") or {}).get("payment_method_id") or "card")


# ── Lifecycle ──────────────────────────────────────────────────────────────────

def issue(db: Session, owner_id: str, snap: dict, guest: bool) -> PurchaseConsent:
    """A new authorization request for this exact order; earlier open ones for it are superseded."""
    pay_ref = pay_ref_of(snap)
    db.execute(update(PurchaseConsent)
               .where(PurchaseConsent.checkout_id == snap["checkout_id"], PurchaseConsent.owner_id == owner_id,
                      PurchaseConsent.status.in_(("issued", "approved")))
               .values(status="superseded"))
    now = time.time()
    row = PurchaseConsent(consent_id=f"pc_{secrets.token_urlsafe(12)}", owner_id=owner_id,
                          checkout_id=snap["checkout_id"], checkout_version=int(snap["checkout_version"]),
                          amount_cents=round(float(snap["total"]) * 100), currency=snap.get("currency") or "USD",
                          pay_ref=pay_ref[:60], fingerprint=(snap.get("checkout_hash") or "")[:16],
                          merchant=MERCHANT, level=level_for(db, owner_id, pay_ref, guest), status="issued",
                          created_at=now, expires_at=now + TTL)
    db.add(row)
    db.commit()
    return row


def find(db: Session, consent_id: Optional[str]) -> Optional[PurchaseConsent]:
    return db.query(PurchaseConsent).filter_by(consent_id=str(consent_id or "")[:40]).first() if consent_id else None


def check(db: Session, claims: Optional[dict], owner_id: str, checkout_id: str, snap: dict) -> PurchaseConsent:
    """The live consent row behind these token claims, for this owner's live order, or ConsentError."""
    if not claims:
        raise ConsentError("AUTH_INVALID", "That authorization isn't valid (or it expired). Call review_order "
                           "again and ask the shopper to authorize the new summary.")
    row = find(db, claims.get("jti"))
    if (row is None or row.owner_id != owner_id or row.checkout_id != checkout_id or claims.get("sub") != owner_id
            or claims.get("chk") != checkout_id or claims.get("mer") != MERCHANT or row.merchant != MERCHANT):
        raise ConsentError("AUTH_NOT_FOR_THIS_ORDER", "That authorization isn't valid for this order. Call review_order "
                           "and ask the shopper to authorize this order.")
    if row.status == "used":
        raise ConsentError("AUTH_USED", "That authorization was already used. Call review_order for a new one.")
    if row.status == "superseded":
        raise ConsentError("AUTH_SUPERSEDED", "A newer summary replaced that authorization. Use the latest "
                           "review_order authorization (ask the shopper about that summary).")
    if row.status == "declined":
        raise ConsentError("AUTH_DECLINED", "The shopper declined this order. Nothing was charged; review_order "
                           "again only if they ask to buy.")
    if row.expires_at < time.time():
        raise ConsentError("AUTH_EXPIRED", "That authorization expired, so nothing was charged. Call review_order "
                           "and ask the shopper to authorize again.")
    same = (row.checkout_version == snap["checkout_version"] and row.amount_cents == round(snap["total"] * 100)
            and row.currency == snap["currency"] and row.fingerprint == (snap.get("checkout_hash") or "")[:16]
            and row.pay_ref == pay_ref_of(snap)[:60]
            and (claims.get("ver"), claims.get("amt"), claims.get("cur"), claims.get("pay"))
            == (row.checkout_version, row.amount_cents, row.currency, row.pay_ref))
    if not same:
        raise ConsentError("AUTH_MISMATCH", "The order changed after the shopper authorized it (price, delivery, "
                           "address or card), so nothing was charged. Call review_order and ask them to "
                           "authorize the new summary.")
    return row


def note(db: Session, row: PurchaseConsent, **evidence) -> None:
    row.evidence = json.dumps({**(json.loads(row.evidence) if row.evidence else {}), **evidence})[:2000]
    db.commit()


def approve(db: Session, row: PurchaseConsent, via: str) -> bool:
    """The customer's own approval (step-up page). Only an open, unexpired request can be approved."""
    if row.status != "issued" or row.expires_at < time.time():
        return False
    done = db.execute(update(PurchaseConsent).where(PurchaseConsent.id == row.id, PurchaseConsent.status == "issued")
                      .values(status="approved", approved_at=time.time())).rowcount
    db.commit()
    db.refresh(row)
    if done:
        note(db, row, step_up=via)
    return bool(done)


def decline(db: Session, owner_id: str, checkout_id: str, via: str) -> int:
    n = db.execute(update(PurchaseConsent)
                   .where(PurchaseConsent.checkout_id == checkout_id, PurchaseConsent.owner_id == owner_id,
                          PurchaseConsent.status.in_(("issued", "approved")))
                   .values(status="declined")).rowcount
    db.commit()
    return n


def consume(db: Session, row: PurchaseConsent) -> None:
    """Claim the authorization once, right before payment (a concurrent second claim loses)."""
    want = ("approved",) if row.level == "customer_step_up" else ("issued", "approved")
    done = db.execute(update(PurchaseConsent).where(PurchaseConsent.id == row.id, PurchaseConsent.status.in_(want))
                      .values(status="used", used_at=time.time())).rowcount
    db.commit()
    if not done:
        raise ConsentError("AUTH_USED", "That authorization was already used (or isn't approved). Call "
                           "review_order for a new one.")
    db.refresh(row)


def view(row: PurchaseConsent) -> dict:
    return {"consent_id": row.consent_id, "status": row.status, "level": row.level,
            "proof": LEVEL_TEXT.get(row.level), "expires_in_seconds": max(0, int(row.expires_at - time.time()))}


__all__ = ["ConsentError", "classify_reply", "level_for", "issue", "check", "approve", "decline", "consume", "note",
           "find", "view", "MERCHANT", "TTL"]
