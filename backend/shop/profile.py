"""
Customer profile — saved shipping addresses and saved cards.

Cards are stored masked only: network, last 4, expiry and an opaque token
reference. CH-6: ShopSphere never receives a card number or CVC at all — new
cards are given on the (simulated) payment provider's own page, and only a card
the customer chose to save there is kept, as the provider's token.
"""
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.schema import Address, PaymentMethod
from backend.shop.cards import payment_card_fields


class ProfileError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def address_dict(a: Address) -> dict:
    return {
        "address_id": a.address_id, "label": a.label, "full_name": a.full_name,
        "line1": a.line1, "line2": a.line2, "city": a.city, "state": a.state,
        "postal_code": a.postal_code, "country": a.country, "is_default": bool(a.is_default),
        "display": f"{a.full_name}, {a.line1}{', ' + a.line2 if a.line2 else ''}, {a.city}, {a.state} {a.postal_code}",
    }


def card_dict(c: PaymentMethod) -> dict:
    """Masked card view. CH-1 adds issuer and network as separate codes (PaymentCard
    fields from backend/shop/cards.py); "brand" stays for older callers. No token."""
    return {
        "payment_method_id": c.payment_method_id, "brand": c.brand, "last4": c.last4,
        "exp_month": c.exp_month, "exp_year": c.exp_year, "cardholder_name": c.cardholder_name,
        "is_default": bool(c.is_default), "display": f"{c.brand} •••• {c.last4}",
        "saved": c.saved is not False,
        **payment_card_fields(c),
    }


def list_addresses(db: Session, user_id: str) -> list[Address]:
    return (db.query(Address).filter_by(user_id=user_id)
            .order_by(Address.is_default.desc(), Address.id.asc()).all())


def list_cards(db: Session, user_id: str) -> list[PaymentMethod]:
    """Saved cards only — a card tokenized for one order isn't listed (Phase 10)."""
    return (db.query(PaymentMethod).filter_by(user_id=user_id).filter(PaymentMethod.saved.isnot(False))
            .order_by(PaymentMethod.is_default.desc(), PaymentMethod.id.asc()).all())


def _delete_saved(db: Session, model, id_field: str, user_id: str, item_id: str, missing: ProfileError) -> Optional[str]:
    item = db.query(model).filter(getattr(model, id_field) == item_id, model.user_id == user_id).first()
    if item is None:
        raise missing
    db.delete(item)
    db.flush()
    rest = db.query(model).filter_by(user_id=user_id)
    if model is PaymentMethod:
        rest = rest.filter(PaymentMethod.saved.isnot(False))     # one-time cards never become the default
    rest = rest.order_by(model.is_default.desc(), model.id.asc()).all()
    if rest and not any(r.is_default for r in rest):
        rest[0].is_default = True                     # the next saved one becomes the default
    db.commit()
    return getattr(rest[0], id_field) if rest else None


def delete_address(db: Session, user_id: str, address_id: str) -> Optional[str]:
    """Remove one of the customer's saved addresses. Past orders keep their own
    copy (orders.ship_to_json). Returns the new default address id, if any."""
    return _delete_saved(db, Address, "address_id", user_id, address_id,
                         ProfileError("UNKNOWN_ADDRESS", "That address isn't saved on your account.", 404))


def delete_card(db: Session, user_id: str, payment_method_id: str) -> Optional[str]:
    """Remove a saved card (and its vault token). Past orders keep the brand and
    last 4 they were paid with. Returns the new default card id, if any."""
    return _delete_saved(db, PaymentMethod, "payment_method_id", user_id, payment_method_id,
                         ProfileError("UNKNOWN_CARD", "That card isn't saved on your account.", 404))


def add_address(db: Session, user_id: str, *, full_name: str, line1: str, city: str, state: str,
                postal_code: str, line2: Optional[str] = None, label: Optional[str] = None,
                country: str = "US", make_default: bool = False) -> Address:
    for field, value in (("full_name", full_name), ("line1", line1), ("city", city),
                         ("state", state), ("postal_code", postal_code)):
        if not (value or "").strip():
            raise ProfileError("MISSING_FIELD", f"Please fill in {field.replace('_', ' ')}.")
    if not re.fullmatch(r"[A-Za-z]{2}", state.strip()):
        raise ProfileError("INVALID_STATE", "State should be a 2-letter code, like TX.")
    if any(len(v or "") > 120 for v in (full_name, line1, line2, city)):
        raise ProfileError("FIELD_TOO_LONG", "One of the address fields is too long.")
    if not re.fullmatch(r"\d{5}(-\d{4})?", postal_code.strip()):
        raise ProfileError("INVALID_POSTAL_CODE", "ZIP code should look like 78704.")
    first = not list_addresses(db, user_id)
    if make_default or first:
        db.query(Address).filter_by(user_id=user_id).update({"is_default": False})
    a = Address(address_id=f"ADDR_{uuid.uuid4().hex[:10].upper()}", user_id=user_id, label=label or "Home",
                full_name=full_name.strip(), line1=line1.strip(), line2=(line2 or "").strip() or None,
                city=city.strip(), state=state.strip().upper(), postal_code=postal_code.strip(),
                country=country, is_default=make_default or first)
    db.add(a)
    db.commit()
    return a


def save_provider_card(db: Session, user_id: str, *, network: str, last4: str, payment_token: str,
                       issuer: str = "UNKNOWN", exp_month: int = 12, exp_year: Optional[int] = None,
                       cardholder_name: Optional[str] = None, make_default: bool = False) -> PaymentMethod:
    """CH-6: keep a card the customer chose to save on the payment provider's page — as
    the provider's OPAQUE token plus the network and last 4 digits. No card number or
    CVC ever reaches ShopSphere, so there is nothing else to store. The provider also hands
    over the expiry and cardholder name for display, and only when the customer ticked
    "save"; without them the expiry is a display placeholder."""
    from backend.shop.cards import network_display, normalize_network
    first = not list_cards(db, user_id)
    make_default = make_default or first
    if make_default:
        db.query(PaymentMethod).filter_by(user_id=user_id).update({"is_default": False})
    card = PaymentMethod(
        payment_method_id=f"PM_{uuid.uuid4().hex[:10].upper()}", user_id=user_id,
        brand=network_display(normalize_network(network)), last4=last4, exp_month=exp_month,
        exp_year=exp_year or datetime.now(timezone.utc).year + 4, cardholder_name=cardholder_name,
        token_ref=payment_token, behaviour="approve", is_default=make_default, saved=True,
        issuer=issuer, status="active", source="provider_token",
    )
    db.add(card)
    db.commit()
    return card
