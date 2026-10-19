"""
Payment routing (CH-5) — the ONE decision for how a checkout is paid. Made by
the server from the checkout's selected way to pay — never by the browser or
the LLM. Network-aware: a credit card is not assumed to be Visa.

  route               legacy "path"      what happens
  wallet              wallet             ShopSphere Wallet balance (internal, no gateway)
  saved_card          standard           the saved card's opaque token (tok_…) → mock gateway →
                                         simulated <network> network → simulated <issuer> issuer.
                                         Every network: Visa, Mastercard, Amex, Discover.
  vic                 vic                Visa agent payment (VIC-inspired, simulated): ONLY a Visa-network
                                         card flagged agent-pay capable, with an ACTIVE agent token
  vic_setup_required  vic_setup_required that Visa card, agent payments not set up yet
  provider_handoff    provider_handoff   a new card through the simulated secure payment provider (CH-6)
  none                none               nothing chosen yet

"path" keeps the older names for the website, Talkshop and tests; "route" is the
generic name. Mastercard, Amex and Discover never reach the VIC path.
"""
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.schema import CardAccount, Checkout, PaymentMethod
from backend.shop import cards
from backend.vic.common import short

LEGACY_PATH = {"saved_card": "standard"}


card_label = cards.card_label


def _vic_capable(acct: Optional[CardAccount], pm: PaymentMethod) -> bool:
    """Visa agent payments (VIC-inspired) are a Visa-NETWORK capability (CH-9): the card
    must be on the Visa network AND flagged agent-pay capable by its (simulated) issuer.
    The issuer itself is never checked here — and a Mastercard, Amex or Discover card can
    never take this path, whatever its flags say."""
    return bool(acct and acct.agent_pay_capable and cards.normalize_network(pm.brand) == "VISA"
                and cards.normalize_network(acct.network) == "VISA" and acct.account_type == "credit_card"
                and acct.status == "active")


def route(db: Session, co: Checkout) -> dict:
    if co.pay_with == "wallet":
        return {"path": "wallet", "route": "wallet", "label": "ShopSphere Wallet"}
    if co.pay_with == "provider":
        return {"path": "provider_handoff", "route": "provider_handoff",
                "label": "New card · Simulated Secure Payment Provider",
                "issuer": "Card issuer (simulated)", "page_owner": "Simulated Secure Payment Provider"}
    card = db.query(PaymentMethod).filter_by(payment_method_id=co.payment_method_id).first() \
        if co.payment_method_id else None
    if not card:
        return {"path": "none", "route": "none"}
    acct = db.query(CardAccount).filter_by(user_id=co.user_id, payment_method_id=card.payment_method_id).first()
    network = cards.normalize_network(card.brand)
    masked = {"brand": card.brand, "last4": card.last4, "issuer": cards.card_issuer(card, acct), "network": network,
              "display": card_label(card, acct)}
    issuer = f"{acct.issuer} (simulated)" if acct else "Card issuer (simulated)"
    base = {"card": masked, "network": network, "issuer": issuer}
    if _vic_capable(acct, card):
        from backend.vic import enrollment
        tok = enrollment.active_for_payment_method(db, co.user_id, card.payment_method_id)
        if tok:
            return {"path": "vic", "route": "vic", "label": "Visa agent payment (VIC-inspired, simulated)", **base,
                    "card_account_id": acct.account_id, "agent_token": short(tok.agent_token_ref),
                    "agent_token_status": "ACTIVE"}
        return {"path": "vic_setup_required", "route": "vic_setup_required", "label": "Visa agent payments not set up",
                **base, "card_account_id": acct.account_id,
                "agent_token_status": enrollment.status(db, co.user_id, acct.account_id)}
    return {"path": "standard", "route": "saved_card",
            "label": f"Saved card · {masked['display']} ({cards.network_display(network)} network)", **base}
