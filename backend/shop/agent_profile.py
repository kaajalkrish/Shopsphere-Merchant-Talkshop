"""
Agent profile providers: a shopper who is a GUEST at ShopSphere may still have details their AI
assistant (or a wallet / identity provider / token vault they use) is authorized to share. The
merchant asks a provider for exactly what one order needs, only after the shopper consents in
this chat, and never sees a card number.

  AgentProfileProvider               the interface: a coarse preview (before consent), contact,
                                     shipping addresses, masked payment instruments with opaque
                                     references
  DemoAgentProfileProvider           FICTIONAL, local: backend/config/demo_guest_profile.json and
                                     sandbox tokens (backend/shop/guest_wallet.py). Clearly labelled
                                     simulated. It stands for "a profile this shopper authorized";
                                     it is not evidence of who the shopper is, so it is offered per
                                     chat session and used only under that session's own grant.
  ExternalAuthorizedProfileProvider  the slot for a real, supported provider. Claude's connector
                                     platform does NOT hand merchants a user's saved addresses or
                                     cards today, so this stays UNCONFIGURED unless a client for a
                                     real provider is registered (register_external_client). No
                                     Claude API is assumed or called.

Consent (ProfileGrant): one per chat session (subject = that chat's visitor id), scoped
("contact,shipping" and/or "payment_instruments"), 2 hours. Another chat, another visitor, never
sees this one's grant, chosen address or remembered preferences. Preferences (which address, which
card reference, delivery speed) are stored only when the shopper asks to remember them, and are
references, never card data.

Fallback when no provider is available: the guest types name, email and address once
(update_guest_checkout) and pays with a new card on the simulated secure payment provider's page.
"""
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Optional, Protocol

from sqlalchemy.orm import Session

from backend.db.schema import PaymentMethod, ProfileGrant
from backend.shop import cards, guest_wallet

GRANT_TTL = 2 * 3600
SHIPPING_SCOPES = ("contact", "shipping")
PAYMENT_SCOPE = "payment_instruments"
REMEMBERABLE = ("address_id", "payment_method_id", "delivery")


class ProviderNotConfigured(Exception):
    pass


@dataclass
class Address:
    address_id: str
    label: str
    full_name: str
    line1: str
    city: str
    state: str
    postal_code: str
    line2: Optional[str] = None

    def area(self) -> str:                  # what may be said BEFORE consent
        return f"{self.city}, {self.state}"

    def display(self) -> str:               # after consent, to confirm with the shopper
        street = ", ".join(x for x in (self.line1, self.line2) if x)
        return f"{street}, {self.city}, {self.state} {self.postal_code}"


@dataclass
class Instrument:
    """A payment credential the provider holds, as the merchant may see it: network, last 4 and an
    opaque reference (never a card number, expiry or CVC)."""
    payment_method_id: str
    network: str
    last4: str
    display: str
    simulated: bool = True

    def view(self) -> dict:
        return {"payment_method_id": self.payment_method_id, "display": self.display, "network": self.network,
                "last4": self.last4}


@dataclass
class Contact:
    full_name: str
    email: str


class AgentProfileProvider:
    provider_id = "base"
    name = "Agent profile provider"
    simulated = True

    def configured(self) -> bool:
        return False

    def describe(self) -> dict:
        return {"provider_id": self.provider_id, "name": self.name, "simulated": self.simulated}

    # before consent: coarse only (city/state, card networks) — enough to ask the question
    def preview(self, subject: str) -> Optional[dict]:
        raise NotImplementedError

    # after consent (a grant with the right scope)
    def contact(self, subject: str) -> Contact:
        raise NotImplementedError

    def addresses(self, subject: str) -> list[Address]:
        raise NotImplementedError

    def instruments(self, db: Session, subject: str) -> list[Instrument]:
        raise NotImplementedError


class DemoAgentProfileProvider(AgentProfileProvider):
    """The fictional demo profile (sandbox). Data comes from the server's own config file; nothing is
    read from Claude or any real wallet."""
    provider_id = "demo"
    name = "Demo agent profile (fictional, sandbox)"
    simulated = True

    def __init__(self, profile: Optional[dict] = None):
        self._profile = profile

    def _data(self) -> Optional[dict]:
        return self._profile or guest_wallet.load()

    def configured(self) -> bool:
        return self._data() is not None

    def addresses(self, subject: str) -> list[Address]:
        p = self._data()
        if not p:
            raise ProviderNotConfigured(self.name)
        raw = p.get("shipping_addresses") or [{"id": "home", "label": "Home", **p["shipping_address"]}]
        return [Address(address_id=a.get("id") or f"addr{i + 1}", label=a.get("label") or f"Address {i + 1}",
                        full_name=a.get("full_name") or p["contact"]["full_name"], line1=a["line1"],
                        line2=a.get("line2"), city=a["city"], state=a["state"], postal_code=a["postal_code"])
                for i, a in enumerate(raw)]

    def contact(self, subject: str) -> Contact:
        p = self._data()
        if not p:
            raise ProviderNotConfigured(self.name)
        return Contact(full_name=p["contact"]["full_name"], email=p["contact"]["email"])

    def preview(self, subject: str) -> Optional[dict]:
        p = self._data()
        if not p:
            return None
        return {"areas": [a.area() for a in self.addresses(subject)],
                "payment_networks": [cards.network_display(cards.normalize_network(c["network"]))
                                     for c in p["payment_methods"]]}

    def instruments(self, db: Session, subject: str) -> list[Instrument]:
        p = self._data()
        if not p:
            raise ProviderNotConfigured(self.name)
        return [Instrument(**{k: v for k, v in guest_wallet.view(pm).items()}, simulated=True)
                for pm in guest_wallet.payment_methods(db, subject, p)]


class ExternalProfileClient(Protocol):
    """What a real provider integration would implement (an agent wallet, identity provider or token
    vault the shopper authorized). Every call is for one subject the provider has a consent for."""

    name: str

    def preview(self, subject: str) -> Optional[dict]: ...
    def contact(self, subject: str) -> dict: ...                  # {full_name, email}
    def addresses(self, subject: str) -> list[dict]: ...          # Address fields
    def instruments(self, subject: str) -> list[dict]: ...        # {reference, network, last4}


class ExternalAuthorizedProfileProvider(AgentProfileProvider):
    """A real, supported provider. Unconfigured unless a client is registered: the Claude connector
    platform doesn't offer merchants a shopper's saved addresses or cards, so ShopSphere doesn't
    pretend it does."""
    provider_id = "external"
    simulated = False
    SOURCE = "agent_provider"

    def __init__(self, client: Optional[ExternalProfileClient] = None):
        self.client = client
        self.name = getattr(client, "name", None) or "External authorized profile provider (not configured)"

    def configured(self) -> bool:
        return self.client is not None

    def _need(self):
        if not self.client:
            raise ProviderNotConfigured(self.name)
        return self.client

    def preview(self, subject: str) -> Optional[dict]:
        return self._need().preview(subject) if self.client else None

    def contact(self, subject: str) -> Contact:
        c = self._need().contact(subject)
        return Contact(full_name=c["full_name"], email=c["email"])

    def addresses(self, subject: str) -> list[Address]:
        return [Address(**{k: a.get(k) for k in Address.__dataclass_fields__}) for a in self._need().addresses(subject)]

    def instruments(self, db: Session, subject: str) -> list[Instrument]:
        """The provider's opaque references become single-use, unsaved payment methods of this guest.
        ShopSphere keeps only a keyed hash of the reference (token_ref "tok_ext_…"), never the reference
        itself; the model sees network + last 4. In this demo the simulated gateway charges that token;
        a real integration would hand the charge to the provider, which holds the credential."""
        out = []
        for it in self._need().instruments(subject):
            secret = (os.getenv("JWT_SECRET_KEY") or "demo-jwt-secret-replace-in-production").encode()
            ref = "tok_ext_" + hmac.new(secret, f"{self.name}:{it['reference']}".encode(), hashlib.sha256).hexdigest()[:32]
            pm = db.query(PaymentMethod).filter_by(user_id=subject, token_ref=ref, source=self.SOURCE).first()
            if not pm:
                pm = PaymentMethod(payment_method_id=f"ext_{secrets.choice('abcdef')}{secrets.token_hex(5)}", user_id=subject,
                                   brand=cards.network_display(cards.normalize_network(it["network"])), issuer=None,
                                   status="active", source=self.SOURCE, last4=str(it["last4"])[-4:], exp_month=12,
                                   exp_year=2099, cardholder_name="", token_ref=ref, behaviour="approve", saved=False)
                db.add(pm)
                db.commit()
            out.append(Instrument(payment_method_id=pm.payment_method_id, network=pm.brand, last4=pm.last4,
                                  display=f"{pm.brand} •••• {pm.last4}", simulated=False))
        return out


_external_client: Optional[ExternalProfileClient] = None


def register_external_client(client: Optional[ExternalProfileClient]) -> None:
    """Plug in a real provider's client (or None to remove it). Nothing registers one by default."""
    global _external_client
    _external_client = client


def provider() -> Optional[AgentProfileProvider]:
    """The provider this server uses for guests, or None (then the typed-details fallback applies).
    AGENT_PROFILE_PROVIDER: "demo" (default), "external", or "off"."""
    which = (os.getenv("AGENT_PROFILE_PROVIDER") or "demo").strip().lower()
    if which in ("off", "none", "0", "false"):
        return None
    if which == "external":
        ext = ExternalAuthorizedProfileProvider(_external_client)
        return ext if ext.configured() else None
    demo = DemoAgentProfileProvider()
    return demo if demo.configured() else None


def status() -> dict:
    """What a guest may be told about profile sharing on this server (no personal data)."""
    which = (os.getenv("AGENT_PROFILE_PROVIDER") or "demo").strip().lower()
    p = provider()
    if p:
        return {"available": True, **p.describe()}
    reason = ("An external profile provider is selected but none is configured on this server."
              if which == "external" else "No profile provider is enabled on this server.")
    return {"available": False, "reason": reason}


def is_profile_instrument(db: Session, user_id: str, payment_method_id: Optional[str]) -> bool:
    """A payment method a profile provider issued to this guest (demo sandbox or external reference)."""
    return bool(payment_method_id) and db.query(PaymentMethod).filter(
        PaymentMethod.payment_method_id == payment_method_id, PaymentMethod.user_id == user_id,
        PaymentMethod.source.in_((guest_wallet.SOURCE, ExternalAuthorizedProfileProvider.SOURCE))).first() is not None


# ── Consent: one grant per chat session ────────────────────────────────────────

def active_grant(db: Session, subject: str, provider_id: str) -> Optional[ProfileGrant]:
    g = (db.query(ProfileGrant).filter_by(subject=subject, provider=provider_id, status="active")
         .order_by(ProfileGrant.id.desc()).first())
    return g if g and g.expires_at > time.time() else None


def scopes(g: Optional[ProfileGrant]) -> set[str]:
    return set((g.scopes or "").split(",")) - {""} if g else set()


def grant(db: Session, subject: str, provider_id: str, wanted: set[str]) -> ProfileGrant:
    """Record the shopper's consent (adds scopes to this session's grant)."""
    g = active_grant(db, subject, provider_id)
    if g:
        g.scopes = ",".join(sorted(scopes(g) | wanted))
    else:
        g = ProfileGrant(grant_id=f"pg_{secrets.token_hex(8)}", subject=subject, provider=provider_id,
                         scopes=",".join(sorted(wanted)), status="active", created_at=time.time(),
                         expires_at=time.time() + GRANT_TTL)
        db.add(g)
    db.commit()
    return g


def revoke(db: Session, subject: str) -> int:
    n = db.query(ProfileGrant).filter_by(subject=subject, status="active").update({"status": "revoked"})
    db.commit()
    return n


def preferences(g: Optional[ProfileGrant]) -> dict:
    try:
        return json.loads(g.preferences) if g and g.preferences else {}
    except ValueError:
        return {}


def remember(db: Session, g: ProfileGrant, **prefs) -> dict:
    """Store non-sensitive choices (references only) when the shopper asked to remember them."""
    now = {**preferences(g), **{k: str(v)[:60] for k, v in prefs.items() if k in REMEMBERABLE and v}}
    g.preferences = json.dumps(now)
    db.commit()
    return now


def require(g: Optional[ProfileGrant], scope: str) -> None:
    if scope not in scopes(g):
        raise PermissionError(f"The shopper hasn't agreed to share {scope.replace('_', ' ')} in this chat.")


def as_dict(a: Address) -> dict:
    return asdict(a)


__all__ = ["AgentProfileProvider", "DemoAgentProfileProvider", "ExternalAuthorizedProfileProvider",
           "ExternalProfileClient", "ProviderNotConfigured", "Address", "Instrument", "Contact", "provider",
           "status", "register_external_client", "grant", "active_grant", "revoke", "remember", "preferences",
           "scopes", "require", "is_profile_instrument"]
