"""
Catalog, search and inventory — ShopSphere's single source of product facts.

search() applies hard filters (department, category, gender, budget, colour,
size-in-stock) and then ranks by relevance to the shopper's words, so the
caller (website or Talkshop) only ever presents what the catalog returned.
"""
import re
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.schema import Product, ProductVariant

SORTS = ("relevance", "price_asc", "price_desc", "rating", "new")
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "and", "for", "the", "with", "i", "need", "want", "some", "under", "below", "over",
         "shoes", "shoe", "my", "me", "to", "of", "in", "on", "pair", "looking"}


def _tags(p: Product) -> list[str]:
    return [t for t in (p.tags or "").split(",") if t]


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def _colors(p: Product) -> list[dict]:
    """Colours in catalog order, each with its photo and whether any size is in stock."""
    seen: dict[str, dict] = {}
    for v in p.variants:
        c = seen.setdefault(v.color, {"name": v.color, "hex": v.color_hex, "image_url": v.image_url, "in_stock": False})
        c["in_stock"] = c["in_stock"] or v.stock > 0
    return list(seen.values())


def _sizes(p: Product) -> list[str]:
    out: list[str] = []
    for v in p.variants:
        if v.size is not None and v.size not in out:
            out.append(v.size)
    return out


def product_summary(p: Product) -> dict:
    """What a product card needs. Older fields (size, color, inventory, available…)
    are kept so pre-Demo-1 callers keep working."""
    stock = sum(v.stock for v in p.variants)
    colors = _colors(p)
    return {
        "product_id": p.product_id,
        "slug": p.slug,
        "merchant_id": p.merchant_id,
        "name": p.name,
        "brand": p.brand,
        "department": p.department,
        "category": p.category,
        "subcategory": p.subcategory,
        "gender": p.gender,
        "description": p.description,
        "tags": _tags(p),
        "price": p.price,
        "currency": p.currency or "USD",
        "rating": p.rating,
        "review_count": p.review_count,
        "is_new": bool(p.is_new),
        "delivery_days": p.delivery_days,
        "option_label": p.option_label or "Size",
        "image_url": p.image_url or (colors[0]["image_url"] if colors else None),
        "colors": colors,
        "sizes": _sizes(p),
        "in_stock": stock > 0,
        # legacy
        "size": p.size,
        "color": p.color,
        "inventory": stock,
        "available": stock > 0,
        "weight_grams": p.weight_grams,
        "cushioning": p.cushioning,
    }


def product_detail(p: Product) -> dict:
    d = product_summary(p)
    d["variants"] = [
        {"sku": v.sku, "size": v.size, "color": v.color, "color_hex": v.color_hex,
         "stock": v.stock, "in_stock": v.stock > 0, "image_url": v.image_url}
        for v in p.variants
    ]
    return d


def _relevance(p: Product, query_words: set[str], phrase: str) -> float:
    tags = _tags(p)
    score = 0.0
    if phrase:
        # whole tag phrase in the request, e.g. "everyday running" — as whole words, so the tag
        # "phone" doesn't match inside "earphone"
        score += sum(5 for t in tags if re.search(rf"{re.escape(t)}", phrase))
    tag_words = {w for t in tags for w in _words(t)}
    score += 2 * len(query_words & tag_words)
    score += 2 * len(query_words & _words(f"{p.name} {p.brand or ''}"))
    score += 1 * len(query_words & _words(p.description or ""))
    score += 1 * len(query_words & _words(f"{p.subcategory or ''} {p.category or ''}"))
    return score


def search(
    db: Session,
    *,
    q: Optional[str] = None,
    department: Optional[str] = None,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    gender: Optional[str] = None,
    brand: Optional[str] = None,
    color: Optional[str] = None,
    size: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    is_new: Optional[bool] = None,
    in_stock: bool = True,
    merchant_id: Optional[str] = None,
    sort: str = "relevance",
    limit: Optional[int] = None,
) -> list[Product]:
    query = db.query(Product)
    if department:
        query = query.filter(Product.department == department)
    if category:
        query = query.filter(Product.category == category)
    if subcategory:
        query = query.filter(Product.subcategory == subcategory)
    if gender in ("women", "men"):                     # adults: their own + unisex (never kids' sizes)
        query = query.filter(Product.gender.in_([gender, "unisex"]))
    elif gender == "kids":
        query = query.filter(Product.gender == "kids")
    if brand:
        query = query.filter(Product.brand.ilike(f"%{brand}%"))
    if min_price is not None:
        query = query.filter(Product.price >= min_price)
    if max_price is not None:
        query = query.filter(Product.price <= max_price)
    if is_new is not None:
        query = query.filter(Product.is_new.is_(is_new))
    if merchant_id:
        query = query.filter(Product.merchant_id == merchant_id)
    products = query.all()

    def variant_ok(v: ProductVariant) -> bool:
        return ((not color or v.color.lower() == color.lower())
                and (not size or v.size == size)
                and (not in_stock or v.stock > 0))
    if color or size or in_stock:
        products = [p for p in products if any(variant_ok(v) for v in p.variants)]

    phrase = (q or "").lower()
    query_words = _words(phrase)
    if sort == "price_asc":
        products.sort(key=lambda p: p.price)
    elif sort == "price_desc":
        products.sort(key=lambda p: -p.price)
    elif sort == "rating":
        products.sort(key=lambda p: (-(p.rating or 0), -(p.review_count or 0)))
    elif sort == "new":
        products.sort(key=lambda p: (not p.is_new, -(p.rating or 0)))
    else:  # relevance, then rating as the tie-break
        scored = [(_relevance(p, query_words, phrase), p) for p in products]
        if query_words and any(score > 0 for score, _ in scored):
            # The shopper's words matched something: don't pad results with
            # products that match none of them (no AirPods for "cozy sweater").
            scored = [(score, p) for score, p in scored if score > 0]
        scored.sort(key=lambda sp: (-sp[0], -(sp[1].rating or 0), -(sp[1].review_count or 0)))
        products = [p for _, p in scored]
    return products[:limit] if limit else products


def availability(p: Product, size: Optional[str] = None, color: Optional[str] = None) -> dict:
    """What can actually be bought: sizes for a colour, colours for a size, and
    the exact SKU once both are chosen — with in-stock alternatives if not."""
    variants = p.variants
    sizes = _sizes(p)
    has_sizes = bool(sizes)
    in_color = [v for v in variants if not color or v.color.lower() == color.lower()]
    in_size = [v for v in variants if not size or v.size == size]
    result = {
        "product_id": p.product_id,
        "option_label": p.option_label or "Size",
        "has_sizes": has_sizes,
        "sizes": [{"size": s, "available": any(v.stock > 0 for v in in_color if v.size == s)} for s in sizes],
        "colors": [{"name": c["name"], "hex": c["hex"], "image_url": c["image_url"],
                    "available": any(v.stock > 0 for v in in_size if v.color == c["name"])}
                   for c in _colors(p)],
        "selected": None,
    }
    if color and (size or not has_sizes):
        match = next((v for v in variants if v.color.lower() == color.lower() and v.size == (size if has_sizes else None)), None)
        if match:
            result["selected"] = {"sku": match.sku, "size": match.size, "color": match.color,
                                  "stock": match.stock, "available": match.stock > 0, "image_url": match.image_url}
        else:
            result["selected"] = {"sku": None, "size": size, "color": color, "stock": 0, "available": False,
                                  "image_url": None}
    return result


def get_product(db: Session, product_id: str) -> Optional[Product]:
    return db.query(Product).filter_by(product_id=product_id).first()


def get_variant(db: Session, sku: str) -> Optional[ProductVariant]:
    return db.query(ProductVariant).filter_by(sku=sku).first()
