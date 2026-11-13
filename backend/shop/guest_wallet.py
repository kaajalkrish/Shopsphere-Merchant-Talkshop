"""
Guest checkout with an authorized profile (Claude connector): ship to and pay from a profile the
shopper already shares with their assistant, instead of typing a form or a card.

In this demo the profile is FICTIONAL and configured on the server
(backend/config/demo_guest_profile.json, or GUEST_DEMO_PROFILE_PATH; GUEST_DEMO_PROFILE=off turns
it off). It never comes from the chat app: nothing about Claude's own account or billing is read.

  shipping   copied onto this one guest order (checkout.set_guest_details), only after the shopper
             says yes; no account is created
  payment    each configured card becomes an opaque SANDBOX token for this guest only
             (PaymentMethod source="sandbox_wallet", saved=False, behaviour="approve"): the
             simulated network and issuer accept it like any card on file. The model sees the
             network and last 4 digits only; no card number exists anywhere.
"""
import json
import os
import secrets
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.schema import PaymentMethod
from backend.shop import checkout as checkout_service
from backend.shop import cards

SOURCE = "sandbox_wallet"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "demo_guest_profile.json"


def load() -> Optional[dict]:
    """The configured profile, or None when it is turned off or unusable."""
    if (os.getenv("GUEST_DEMO_PROFILE") or "").strip().lower() in ("off", "0", "false", "no"):
        return None
    path = Path(os.getenv("GUEST_DEMO_PROFILE_PATH") or DEFAULT_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        contact, address, pms = data["contact"], data["shipping_address"], data["payment_methods"]
        assert contact["full_name"] and contact["email"] and address["line1"] and address["city"]
        assert all(str(p["last4"]).isdigit() and len(str(p["last4"])) == 4 and p["network"] for p in pms)
    except Exception:
        return None
    return data


def _address_line(a: dict) -> str:
    street = ", ".join(x for x in (a.get("line1"), a.get("line2")) if x)
    return f"{street}, {a['city']}, {a['state']} {a['postal_code']}"


def _display(p: dict) -> str:
    return f"{cards.network_display(cards.normalize_network(p['network']))} •••• {p['last4']} (sandbox)"


def offer(profile: Optional[dict] = None) -> dict:
    """What the assistant may tell the shopper before they agree: name, address, card endings."""
    profile = profile or load()
    if not profile:
        return {"available": False}
    from backend.mcp.card_sections import mask_email
    return {"available": True, "source": profile.get("profile_name") or "Demo guest wallet (fictional, sandbox)",
            "name": profile["contact"]["full_name"], "email": mask_email(profile["contact"]["email"]),
            "ship_to": _address_line(profile["shipping_address"]),
            "payment_methods": [_display(p) for p in profile["payment_methods"]]}


def apply_shipping(db: Session, visitor_id: str, checkout_id: str, profile: dict):
    """The shopper said yes: this order ships to the profile's address (this order only)."""
    c, a = profile["contact"], profile["shipping_address"]
    return checkout_service.set_guest_details(
        db, visitor_id, checkout_id, full_name=c["full_name"], email=c["email"], line1=a["line1"],
        line2=a.get("line2"), city=a["city"], state=a["state"], postal_code=a["postal_code"])


def existing(db: Session, visitor_id: str) -> list[PaymentMethod]:
    return (db.query(PaymentMethod).filter_by(user_id=visitor_id, source=SOURCE)
            .order_by(PaymentMethod.id.asc()).all())


def payment_methods(db: Session, visitor_id: str, profile: dict) -> list[PaymentMethod]:
    """This guest's sandbox cards, issued once per guest as opaque tokens."""
    have = existing(db, visitor_id)
    if have:
        return have
    for i, p in enumerate(profile["payment_methods"]):
        db.add(PaymentMethod(
            payment_method_id=f"sbx_{secrets.choice('abcdef')}{secrets.token_hex(5)}", user_id=visitor_id,
            brand=cards.network_display(cards.normalize_network(p["network"])), issuer=None, status="active",
            source=SOURCE, last4=str(p["last4"]), exp_month=int(p.get("exp_month") or 12),
            exp_year=int(p.get("exp_year") or 2030), cardholder_name=profile["contact"]["full_name"],
            token_ref=f"tok_{secrets.token_urlsafe(18)}", behaviour="approve", is_default=(i == 0), saved=False))
    db.commit()
    return existing(db, visitor_id)


def view(pm: PaymentMethod) -> dict:
    return {"payment_method_id": pm.payment_method_id, "display": f"{pm.brand} •••• {pm.last4} (sandbox)",
            "network": pm.brand, "last4": pm.last4}


def is_sandbox_card(db: Session, user_id: str, payment_method_id: Optional[str]) -> bool:
    return bool(payment_method_id) and db.query(PaymentMethod).filter_by(
        payment_method_id=payment_method_id, user_id=user_id, source=SOURCE).first() is not None
