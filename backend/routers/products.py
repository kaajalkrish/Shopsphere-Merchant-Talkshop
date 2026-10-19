"""
ShopSphere catalog API. Browsing is open to everyone. Signed-in customers also
get the points a purchase earns; offers appear only for Chase credit-card holders
(CH-2: offers are a Chase benefit). Checkout and payment are in the shop router.

  GET /api/products                          search + filters + sort
  GET /api/products/{id}                     product detail with every variant (SKU)
  GET /api/products/{id}/availability        sizes/colours in stock, exact SKU check
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine
from backend.auth.dependencies import CurrentUser, get_current_user
from backend.shop import catalog, offers
from backend.shop import eligibility

router = APIRouter(prefix="/api/products", tags=["products"])


def get_db():
    with Session(get_engine()) as session:
        yield session


def _member_offers(db: Session, user: CurrentUser):
    """Server-computed extras for a signed-in customer: their Chase offers ([] unless
    they hold an eligible Chase credit card — offers are a Chase benefit) and points.
    None for a visitor (no extras shown)."""
    if eligibility.check_access(db, user) is not None:
        return None
    return offers.entitled(db, user.user_id)


def _with_member(d: dict, p, member_offers) -> dict:
    """Cardmember extras on a product card, computed by the server: the offers
    that apply and the points a purchase at list price would earn."""
    if member_offers is None:
        d["member"] = None
        return d
    badges = offers.badges_for_product(member_offers, p.department, p.subcategory)
    bonus = sum(int(p.price * o.value) for o in member_offers
                if o.kind == "bonus_points" and offers._matches(o, p.department, p.subcategory))
    d["member"] = {"offers": badges, "points": int(p.price) + bonus}
    return d


@router.get("")
def list_products(
    q: Optional[str] = Query(None, description="Shopper's words, e.g. 'everyday running'"),
    department: Optional[str] = Query(None, description="shoes | clothing | accessories | electronics"),
    category: Optional[str] = Query(None),
    subcategory: Optional[str] = Query(None),
    gender: Optional[str] = Query(None, description="women | men (unisex items are included in both)"),
    brand: Optional[str] = Query(None),
    color: Optional[str] = Query(None),
    size: Optional[str] = Query(None, description="Only products with this size in stock"),
    min_price: Optional[float] = Query(None),
    max_price: Optional[float] = Query(None),
    new: Optional[bool] = Query(None, description="New arrivals only"),
    in_stock: bool = Query(True),
    merchant_id: Optional[str] = Query(None),
    sort: str = Query("relevance", description="relevance | price_asc | price_desc | rating | new"),
    limit: Optional[int] = Query(None, ge=1, le=100),
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    if sort not in catalog.SORTS:
        raise HTTPException(status_code=422, detail=f"sort must be one of {', '.join(catalog.SORTS)}")
    products = catalog.search(
        db, q=q, department=department, category=category, subcategory=subcategory, gender=gender,
        brand=brand, color=color, size=size, min_price=min_price, max_price=max_price, is_new=new,
        in_stock=in_stock, merchant_id=merchant_id, sort=sort, limit=limit,
    )
    member_offers = _member_offers(db, user)
    return [_with_member(catalog.product_summary(p), p, member_offers) for p in products]


@router.get("/{product_id}")
def get_product(product_id: str, db: Session = Depends(get_db), user: CurrentUser = Depends(get_current_user)):
    p = catalog.get_product(db, product_id)
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    return _with_member(catalog.product_detail(p), p, _member_offers(db, user))


@router.get("/{product_id}/availability")
def get_availability(
    product_id: str,
    size: Optional[str] = Query(None),
    color: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    p = catalog.get_product(db, product_id)
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    return catalog.availability(p, size=size, color=color)
