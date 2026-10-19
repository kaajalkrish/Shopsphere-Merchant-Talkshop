"""
RewardsLedgerService (Part 2, Phase 14) — ShopSphere demo points.

Not a real Chase programme. 100 points = $1 off products (D41). Every
movement is a ledger row with a deterministic id per order, so a retry can
never earn, redeem or credit the same thing twice:

  EARN-{order}             base 1 point per $1 paid (written by the payment step)
  BONUS-{offer}-{order}    bonus-points offers
  RDM-{order}              points redeemed at checkout (only once the payment is authorized)
  cashback                 simulated statement credits: offer_redemptions (kind=cashback)
"""
from sqlalchemy.orm import Session

from backend.db.schema import LoyaltyPoints, LoyaltyTransaction, MemberOffer, OfferRedemption, Order


def _account(db: Session, user_id: str) -> LoyaltyPoints:
    lp = db.query(LoyaltyPoints).filter_by(user_id=user_id).first()
    if not lp:
        lp = LoyaltyPoints(user_id=user_id, balance=0, lifetime_points=0)
        db.add(lp)
        db.flush()
    return lp


def balance(db: Session, user_id: str) -> int:
    lp = db.query(LoyaltyPoints).filter_by(user_id=user_id).first()
    return int(lp.balance) if lp else 0


def _entry(db: Session, txn_id: str) -> bool:
    return db.query(LoyaltyTransaction).filter_by(transaction_id=txn_id[:50]).first() is not None


def earn(db: Session, user_id: str, order_id: str, points: int, reason: str, key: str = "EARN") -> bool:
    txn_id = f"{key}-{order_id}"[:50]
    if points <= 0 or _entry(db, txn_id):
        return False
    lp = _account(db, user_id)
    lp.balance += points
    lp.lifetime_points += points
    db.add(LoyaltyTransaction(transaction_id=txn_id, user_id=user_id, order_id=order_id,
                              points_earned=points, reason=reason))
    return True


def redeem(db: Session, user_id: str, order_id: str, points: int) -> int:
    """Spend points for one order, once. Never more than the balance."""
    txn_id = f"RDM-{order_id}"[:50]
    if points <= 0 or _entry(db, txn_id):
        return 0
    lp = _account(db, user_id)
    spent = min(points, lp.balance)
    if spent:
        lp.balance -= spent
        db.add(LoyaltyTransaction(transaction_id=txn_id, user_id=user_id, order_id=order_id,
                                  points_earned=-spent, reason="redeemed"))
    return spent


def ledger(db: Session, user_id: str, limit: int = 50) -> list[dict]:
    """Points and cashback history, newest first, with the order's public id."""
    display = {o.order_id: o.display_id for o in db.query(Order).filter_by(user_id=user_id)}
    rows = []
    for t in db.query(LoyaltyTransaction).filter_by(user_id=user_id):
        kind = "redeemed" if t.points_earned < 0 else ("bonus" if t.transaction_id.startswith("BONUS-")
                                                        else "welcome" if "WELCOME" in t.transaction_id else "earned")
        rows.append({"kind": kind, "points": t.points_earned, "amount": None, "reason": t.reason,
                     "order_id": display.get(t.order_id) or t.order_id,
                     "at": t.created_at.isoformat() if t.created_at else None})
    titles = {o.offer_id: o.title for o in db.query(MemberOffer)}
    for r in db.query(OfferRedemption).filter_by(user_id=user_id):
        if r.kind in ("cashback", "discount_percent", "discount_amount"):
            rows.append({"kind": "cashback" if r.kind == "cashback" else "discount", "points": None,
                         "amount": r.amount, "reason": titles.get(r.offer_id, r.offer_id),
                         "order_id": display.get(r.order_id) or r.order_id,
                         "at": r.created_at.isoformat() if r.created_at else None})
    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    return rows[:limit]


def summary(db: Session, user_id: str) -> dict:
    lp = db.query(LoyaltyPoints).filter_by(user_id=user_id).first()
    cash = sum(r.amount for r in db.query(OfferRedemption).filter_by(user_id=user_id, kind="cashback"))
    saved = sum(r.amount for r in db.query(OfferRedemption).filter(
        OfferRedemption.user_id == user_id, OfferRedemption.kind.in_(("discount_percent", "discount_amount"))))
    bal = int(lp.balance) if lp else 0
    return {"balance": bal, "value": round(bal / 100, 2), "lifetime_points": int(lp.lifetime_points) if lp else 0,
            "cashback_total": round(cash, 2), "offer_savings_total": round(saved, 2),
            "programme": "ShopSphere demo points (not a real Chase rewards programme)",
            "earn_rate": "1 point per $1 for every customer; 2 per $1 when you pay with an eligible Chase credit card",
            "redemption_rate": "100 points = $1 off products, before tax"}
