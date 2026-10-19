"""
Which payment path a checkout takes (Part 2, D38). CH-5: a compatibility facade —
the decision now lives in backend/shop/payment_routing.py (generic, network-aware).

  vic                 Visa agent payment (VIC-inspired, simulated): a VIC-capable Visa
                      card with an ACTIVE agent token
  vic_setup_required  that Visa card, agent payments not active yet
  standard            any other saved card (route "saved_card": Mastercard, Amex,
                      Discover, a Visa without agent payments) → GreenLight DPAT +
                      12 checks + gateway with the card-on-file token
  provider_handoff    a new card through the simulated secure payment provider (CH-6)
  wallet              ShopSphere Wallet balance (internal, no gateway)
  none                no payment method yet
"""
from sqlalchemy.orm import Session

from backend.db.schema import Checkout
from backend.shop import payment_routing


def payment_path(db: Session, co: Checkout) -> dict:
    return payment_routing.route(db, co)
