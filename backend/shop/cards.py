"""
PaymentCard (CH-1) — one network-neutral view of a payment method.

Issuer and network are different things:
  issuer   the bank that issued the card   CHASE, CITI, BOFA, AMEX, … (UNKNOWN if not known)
  network  the card network it runs on     VISA, MASTERCARD, AMEX, DISCOVER, … (UNKNOWN)
A credit card is not automatically Visa, and a Chase card is not automatically Visa.

Codes are upper-case and compared as codes; the UI shows display names. The
last four digits are display metadata only, never proof of anything. The
processor token (token_ref) is never part of a view.

All issuers and cards here are simulated demo fixtures.
"""
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.schema import CardAccount, PaymentMethod

NETWORKS = {"VISA": "Visa", "MASTERCARD": "Mastercard", "AMEX": "American Express", "DISCOVER": "Discover"}
ISSUERS = {"CHASE": "Chase", "CITI": "Citi", "BOFA": "Bank of America", "AMEX": "American Express",
           "CAPITAL_ONE": "Capital One", "DISCOVER": "Discover"}
UNKNOWN = "UNKNOWN"

_NETWORK_ALIASES = {"VISA": "VISA", "MASTERCARD": "MASTERCARD", "MASTER CARD": "MASTERCARD", "MC": "MASTERCARD",
                    "AMEX": "AMEX", "AMERICAN EXPRESS": "AMEX", "DISCOVER": "DISCOVER"}
_ISSUER_ALIASES = {"CHASE": "CHASE", "JPMORGAN CHASE": "CHASE", "CITI": "CITI", "CITIBANK": "CITI",
                   "BOFA": "BOFA", "BANK OF AMERICA": "BOFA", "AMEX": "AMEX", "AMERICAN EXPRESS": "AMEX",
                   "CAPITAL ONE": "CAPITAL_ONE", "CAPITAL_ONE": "CAPITAL_ONE", "DISCOVER": "DISCOVER"}

SOURCES = ("seeded", "issuer_link", "manual_legacy", "provider_token")


def normalize_network(value: Optional[str]) -> str:
    """'Visa' / 'visa' / 'VISA' → 'VISA'; anything unrecognised (e.g. 'Card') → 'UNKNOWN'."""
    return _NETWORK_ALIASES.get(" ".join((value or "").split()).upper(), UNKNOWN)


def normalize_issuer(value: Optional[str]) -> str:
    """'Chase' → 'CHASE'; empty or unrecognised → 'UNKNOWN'."""
    return _ISSUER_ALIASES.get(" ".join((value or "").split()).upper(), UNKNOWN)


def network_display(code: Optional[str]) -> str:
    return NETWORKS.get(normalize_network(code), "Card")


def issuer_display(code: Optional[str]) -> Optional[str]:
    """None when the issuer isn't known (show the network only)."""
    return ISSUERS.get(normalize_issuer(code))


def card_issuer(pm: PaymentMethod, acct: Optional[CardAccount] = None) -> str:
    """The card's issuer code: the card's own field, else the linked issuer account, else UNKNOWN."""
    if pm.issuer:
        return normalize_issuer(pm.issuer)
    return normalize_issuer(acct.issuer) if acct else UNKNOWN


def card_label(pm: PaymentMethod, acct: Optional[CardAccount] = None) -> str:
    """"Chase Visa •••• 1234", "Citi Mastercard •••• 5678", "American Express •••• 9012",
    or just the network when the issuer isn't known ("Visa •••• 4242")."""
    issuer, network = card_issuer(pm, acct), normalize_network(pm.brand)
    net = network_display(network)
    who = issuer_display(issuer)
    name = net if not who or issuer == network else f"{who} {net}"
    return f"{name} •••• {pm.last4}"


def payment_card_fields(pm: PaymentMethod, acct: Optional[CardAccount] = None) -> dict:
    """The CH-1 fields added to every card view (masked metadata only)."""
    issuer = card_issuer(pm, acct)
    network = normalize_network(pm.brand)
    status = pm.status or "active"
    return {
        "issuer": issuer, "issuer_display": issuer_display(issuer),
        "network": network, "network_display": network_display(network),
        "status": status, "source": pm.source, "label": card_label(pm, acct),
        "eligible_for_standard_checkout": status == "active",
    }


def backfill(db: Session) -> int:
    """Fill issuer / status / source on payment methods saved before CH-1.
    issuer: from the linked issuer account, else UNKNOWN. source: issuer_link
    when an issuer account points at the card, else manual_legacy. Returns rows changed."""
    changed = 0
    linked = {a.payment_method_id: a for a in db.query(CardAccount).filter(CardAccount.payment_method_id.isnot(None))}
    for pm in db.query(PaymentMethod).filter((PaymentMethod.issuer.is_(None)) | (PaymentMethod.status.is_(None))
                                             | (PaymentMethod.source.is_(None))):
        acct = linked.get(pm.payment_method_id)
        if pm.issuer is None:
            pm.issuer = normalize_issuer(acct.issuer) if acct else UNKNOWN
        if pm.status is None:
            pm.status = "active"
        if pm.source is None:
            pm.source = "issuer_link" if acct else "manual_legacy"
        changed += 1
    db.commit()
    return changed
