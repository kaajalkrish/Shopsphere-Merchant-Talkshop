"""
Shopkeeper-style discovery (MCP get_shopping_questions): before showing products, a good shop
assistant asks a few questions that actually narrow the choice — who it's for, what it's for,
size, colour, budget — and never offers an option the shop doesn't have.

Given everything the shopper has said so far, plan() works out what is already known, which
questions would still narrow the in-stock products, and the real options for each (from the
catalog and live stock). It doesn't search, show cards or change anything. When few products are
left (or nothing useful remains to ask) it says the assistant is ready to search.
"""
import re
from typing import Optional

from sqlalchemy.orm import Session

from backend.db.schema import Product
from backend.talkshop import parse

DEPARTMENTS = {"shoes": "Shoes (running, sneakers, boots, sandals, dress shoes)",
               "clothing": "Clothing (tees, hoodies, jeans, dresses, jackets)",
               "accessories": "Accessories (bags, backpacks, sunglasses, watches, belts)",
               "electronics": "Electronics (headphones, earbuds, speakers, phones, laptops, smartwatches)"}
WHO_LABEL = {"men": "Men's", "women": "Women's", "unisex": "Unisex", "kids": "Kids'"}
USE_LABEL = {"running": "Road / everyday running", "trail": "Trail running", "sneakers": "Casual sneakers",
             "boots": "Boots", "formal": "Dress shoes", "sandals": "Sandals", "tops": "Tops & tees",
             "dresses": "Dresses", "jeans": "Jeans", "pants": "Pants", "jackets": "Jackets", "knitwear": "Knitwear",
             "shirts": "Shirts"}
_RELATIVE = re.compile(r"\b(son|daughter|boy|girl|grandson|granddaughter|nephew|niece)s?\b")
READY_AT = 3                       # this many products or fewer: show them instead of asking more
_SIZE_ORDER = ["11C", "12C", "13C", "1Y", "2Y", "3Y", "4Y", "5Y", "6Y", "XS", "S", "M", "L", "XL", "XXL"]


def _who(value: Optional[str]) -> Optional[str]:
    """The shopper's answer to "who are they for?" ("Kids'", "a child", "my wife", "unisex")."""
    v = parse.norm(value or "")
    if not v:
        return None
    if "unisex" in v:
        return "unisex"
    return parse.gender_of(v) or ("kids" if re.search(r"\b(child|kid)", v) else None)


def _size_key(s: str):
    if s in _SIZE_ORDER:
        return (0, _SIZE_ORDER.index(s), s)
    try:
        return (1, float(s), s)
    except ValueError:
        return (2, 0, s)


def _category_filter(query, category: Optional[str]):
    from backend.talkshop.tools import _DEPARTMENTS, _SUBCATEGORIES
    if category in _DEPARTMENTS:
        return query.filter(Product.department == _DEPARTMENTS[category])
    if category in _SUBCATEGORIES:
        return query.filter(Product.subcategory == category)
    if category:
        return query.filter(Product.category == category)
    return query


def _in_stock(p: Product, size: Optional[str], color: Optional[str]) -> list:
    return [v for v in p.variants if v.stock > 0 and (not size or (v.size or "").lower() == size.lower())
            and (not color or v.color.lower() == color.lower())]


def _bands(prices: list[float]) -> list[str]:
    lo, hi = min(prices), max(prices)
    if hi - lo < 30:
        return []
    third = (hi - lo) / 3
    a, b = int(round((lo + third) / 10.0) * 10), int(round((lo + 2 * third) / 10.0) * 10)
    if b - a < 20:                                  # too close to be two real choices
        mid = int(round((lo + hi) / 20.0) * 10)
        return [f"Under ${mid}", f"Over ${mid}", "No budget in mind"]
    return [f"Under ${a}", f"${a}–${b}", f"Over ${b}", "No budget in mind"]


def plan(db: Session, request: str, *, for_whom: Optional[str] = None, use: Optional[str] = None,
         size: Optional[str] = None, color: Optional[str] = None, max_price: Optional[float] = None) -> dict:
    text = request or ""
    intent = parse.search_intent(text) or {}
    category = intent.get("category")
    who = _who(for_whom) or parse.gender_of(text)
    relative = _RELATIVE.search(parse.norm(text))
    # "for my son": a child or a grown-up? Ask instead of assuming (unless they already answered).
    child_unclear = bool(relative) and not for_whom and who != "kids"
    if child_unclear:
        who = None
    size = size or parse.asked_size(text)
    max_price = max_price if max_price is not None else intent.get("max_price")
    brand = intent.get("brand")
    words = set(re.findall(r"[a-z]+", parse.norm(text)))
    if not use:
        use = "trail" if "trail" in words else ("running" if {"road", "jogging"} & words else None)

    if not category:
        return {"understood": {k: v for k, v in (("for_whom", who), ("size", size), ("max_price", max_price)) if v},
                "questions": [{"id": "what", "ask": "What are you shopping for today?",
                               "options": list(DEPARTMENTS.values())}],
                "matches_now": None, "ready_to_search": False,
                "next_step": "Ask what they're shopping for, warmly and briefly (ShopSphere sells only these). Then "
                             "call get_shopping_questions again with everything they've said."}

    base = _category_filter(db.query(Product), category)
    if brand:
        base = base.filter(Product.brand.ilike(f"%{brand}%"))
    everyone = base.all()
    known_colors = {v.color.lower(): v.color for p in everyone for v in p.variants}
    if not color:
        color = next((known_colors[w] for w in words if w in known_colors), None)

    def pool(*, g=who, u=use, s=size, c=color, budget=max_price):
        out = []
        for p in everyone:
            if g in ("men", "women") and p.gender not in (g, "unisex"):
                continue
            if g == "kids" and p.gender != "kids":
                continue
            if u and p.subcategory != u:
                continue
            if budget is not None and p.price > budget:
                continue
            if not _in_stock(p, s, c):
                continue
            out.append(p)
        return out

    now = pool()
    questions = []
    genders = {p.gender for p in pool(g=None)}
    if child_unclear and "kids" in genders:
        questions.append({"id": "for_whom", "ask": "Is that for a child (kids' sizes) or an adult?",
                          "options": ["A child (kids' sizes)", "An adult"]})
    elif who is None and len(genders) > 1:
        questions.append({"id": "for_whom", "ask": "Who are they for?",
                          "options": [WHO_LABEL[g] for g in ("women", "men", "unisex", "kids") if g in genders]})
    uses = sorted({p.subcategory for p in pool(u=None) if p.subcategory})
    if not use and len(uses) > 1:
        questions.append({"id": "use", "ask": "What will they mostly be used for?",
                          "options": [USE_LABEL.get(u, u.replace("_", " ").title()) for u in uses]})
    sized = [p for p in pool(s=None) if any(v.size for v in p.variants)]
    if not size and sized and (who or not questions):
        sizes = sorted({v.size for p in sized for v in _in_stock(p, None, color) if v.size}, key=_size_key)
        questions.append({"id": "size", "ask": "What size?" + (" (kids' sizes: C = little kids, Y = big kids)"
                                                              if who == "kids" else ""), "options": sizes})
    if not color:
        counts: dict[str, int] = {}
        for p in pool(c=None):
            for c in {v.color for v in _in_stock(p, size, None)}:
                counts[c] = counts.get(c, 0) + 1
        if len(counts) > 1:
            questions.append({"id": "color", "ask": "Any colour you prefer?",
                              "options": sorted(counts, key=lambda c: -counts[c])[:6] + ["Any colour"]})
    prices = [p.price for p in pool(budget=None)]
    if max_price is None and prices and _bands(prices):
        questions.append({"id": "budget", "ask": "Roughly what budget?", "options": _bands(prices),
                          "price_range": [min(prices), max(prices)]})

    notes = []
    if who == "kids" and not now and "kids" not in {p.gender for p in everyone}:
        kinds = [p.name for p in db.query(Product).filter(Product.gender == "kids").order_by(Product.product_id)]
        notes.append("ShopSphere's kids' section doesn't have this kind of product. It has: " + ", ".join(kinds) + ".")
    elif not now:
        notes.append("Nothing in stock matches all of that; say which choice to relax (size, colour or budget).")
    ready = bool(now) and (len(now) <= READY_AT or not questions)
    understood = {k: v for k, v in (("category", category), ("for_whom", who), ("use", use), ("size", size),
                                    ("color", color), ("max_price", max_price), ("brand", brand)) if v}
    return {
        "understood": understood, "questions": [] if ready else questions, "matches_now": len(now),
        "ready_to_search": ready, "notes": notes,
        "search_args": {"query": text[:200], "category": category, "gender": who, "size": size, "color": color,
                        "max_price": max_price, "brand": brand},
        "next_step": (("Nothing matches: tell the shopper what notes says and offer the closest alternative or "
                       "ask which choice to relax. Don't search other stores unless they ask.") if not now else
                      "Ready: call search_shopsphere with search_args and recommend the best 2-3 like a shopkeeper "
                      "(one line on why each fits what they told you)." if ready else
                      "Ask the FIRST one or two questions only, in one friendly sentence, mentioning the options in "
                      "words. Don't ask anything already understood. When they answer, call get_shopping_questions "
                      "again with everything said so far. If they say \"just show me\", search now."),
    }
