"""
ShopSphere for ChatGPT / Claude — the public MCP server (Streamable HTTP at /mcp).

Search, cart, checkout and payment as interactive cards, reusing Talkshop's catalog search
(backend/talkshop/tools.py) and the shared shop services, so a chat app gets the same products,
prices, stock, totals and payment controls as the website.

Who can do what:
  open             search, product details, the visitor cart, guest checkout, track_order
  guest token      update_guest_checkout, start_guest_payment, finish_guest_payment (one guest
                   checkout, no account; backend/shop/guest_checkout.py)
  signed in        checkout, saved details, approve_and_pay, new-card payment, my_orders (OAuth)
Payment tools are app-only (card buttons): the model can prepare a checkout, never pay.

Deliberately separate from backend/mcp/server.py: that internal server has
user-scoped tools (create_checkout with a default user) that must never be public.
"""
import base64
import json
import os
import re
import secrets
from functools import lru_cache
from io import BytesIO
from typing import Optional

from fastmcp import FastMCP
from fastmcp.apps.app import FastMCPApp
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import ToolResult, _prefab_to_tool_result   # fastmcp==4.0.10 (pinned)
from mcp.types import TextContent
from prefab_ui.actions import OpenLink, SendMessage, SetState, ShowToast
from prefab_ui.actions.mcp import CallTool
from prefab_ui.app import PrefabApp
from prefab_ui.components import (Button, Card, CardContent, CardFooter, Column, Grid, H3, Image, Input, Muted,
                                  Row, Select, SelectOption, Separator, Text)
from prefab_ui.components.control_flow import If
from prefab_ui.rx import ERROR, RESULT, STATE
from sqlalchemy.orm import Session

from backend.db.init_db import CATALOG_IMAGE_DIR, get_engine
from backend.db.schema import User
from backend.routers import auth as account
from backend.mcp.auth import current_customer, optional_customer, read_visitor_cart, visitor_cart_id
from backend.shop import card_approval, notifications, payment_routing, profile, ratelimit
from backend.shop import cart as cart_service
from backend.shop import guest_checkout as guest_service
from backend.shop import orders as order_service
from backend.shop import checkout as checkout_service
from backend.shop import payment as payment_service
from backend.shop.cart import CartError
from backend.shop.checkout import CheckoutError
from backend.vic import instructions as vic_instructions
from backend.vic.common import VicError

from backend.talkshop import parse
from backend.talkshop import tools as talkshop_tools

INSTRUCTIONS = """\
You are the shopper's assistant for ShopSphere, an online store, working like ShopSphere's own
Talkshop assistant: find products, add them to the cart, check out and pay, all here in the chat.
The shopper doesn't need to visit the website.

ShopSphere first, always:
1. For any shopping request (products, sizes, colours, prices, recommendations), call
   search_shopsphere BEFORE anything else, including web search. It shows product cards with
   photos, prices, size and colour pickers and an Add to cart button. Below the cards, briefly
   recommend ONLY those ShopSphere products (name, price, colours in the requested size, one
   line on why each fits). Don't add products from other stores, and don't send the shopper
   to the website to look at them.
2. ShopSphere has priority, the web is the fallback. If search_shopsphere finds no match
   (found=false), say so plainly in one line, then search the web yourself right away (don't
   ask first) and show what you find as web results from other stores. Never present web
   results as ShopSphere products and never offer to buy them through ShopSphere. If the
   shopper asks to compare with other stores, you may also search the web.
3. Never invent ShopSphere products, prices, sizes or stock: use only what the tools return.
   get_shopsphere_product gives details or checks another size or colour.

Buying. The cart needs no sign-in. At checkout the shopper chooses how to buy:
4. When the shopper picks something ("add the black one in size 10"), call add_to_cart with the
   product_id, size and colour; ask for whatever is missing. They can also tap Add to cart on a card.
   Before sign-in, results carry a visitor_cart id: pass that same visitor_cart to every later
   search_shopsphere, add_to_cart, show_cart, checkout and guest_checkout call in this chat, so
   it stays one cart.
4a. A shopper who isn't signed in has three choices at checkout (the cart card shows them):
   sign in to their ShopSphere account, create a new account, or check out as a guest.
   - Sign in or create an account: call checkout with visitor_cart. The connector opens
     ShopSphere's sign-in page, which also has "Create an account"; the cart comes along.
   - Guest: call guest_checkout with visitor_cart. No account and no sign-in. The guest types
     their name, email and shipping address ON the guest checkout card (never ask for them in the
     chat), then pays with a new card on the secure payment page. If they say they finished
     paying there, call finish_guest_payment with the guest_token.
   If the shopper hasn't said which, ask which they prefer; don't pick for them.
4b. Order tracking: signed-in customers use my_orders. Anyone (guests too) can track with
   track_order: ask for the Order ID and the email used at checkout.
5. show_cart shows the cart card; checkout shows the review card with Chase offers, points,
   delivery, tax, total, address and saved card. Pass delivery="express" or use_points=true when
   asked.
6. Paying is ONLY the shopper's own Approve & pay tap on the checkout card, followed by the
   simulated passkey. You cannot pay and must never say an order is paid unless they tell you it
   went through; then my_orders shows the order and its tracking.
7. A new card (or a shopper with no saved card) is paid on the secure payment page: on the
   checkout card they choose "a new card", tap Approve & pay, open the secure page in their
   browser, type the card there (they can tick Save), then tap "I've finished paying". Never ask
   for card numbers, expiry or CVC in the chat; if they type one, tell them not to and point them
   to the secure page. If they say they finished on that page, call finish_new_card_payment.
8. New shoppers create their ShopSphere account on the sign-in page ("Create an account"), with
   their shipping address.
Payments, card networks and issuers in this demo are simulated.
"""

mcp = FastMCP("ShopSphere", instructions=INSTRUCTIONS)

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
# Departments, sub-categories and the product kinds Talkshop's parser recognises.
_CATEGORIES = {"shoes", "clothing", "electronics", "accessories", "earbuds", "headphones", "speakers",
               "smartwatches", "fitness_trackers", "running_shoes", "boots", "sneakers", "watches", "phones",
               "laptops", "sunglasses", "bags"}
_STOP = {"the", "and", "for", "with", "size", "any", "some", "show", "find", "need", "want", "looking", "pair",
         "please", "under", "below", "cheap", "good", "best", "new", "buy", "get", "men", "women", "mens", "womens"}


def _site() -> str:
    """Public storefront for links and photos (photos are served by the frontend)."""
    return (os.getenv("MCP_PUBLIC_SITE_URL") or os.getenv("FRONTEND_URL") or "http://localhost:5173").rstrip("/")


def _abs(path: Optional[str]) -> Optional[str]:
    return f"{_site()}{path}" if path and path.startswith("/") else path


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def _stem(w: str) -> str:
    return w[:-2] if w.endswith("es") and len(w) > 4 else w[:-1] if w.endswith("s") and len(w) > 3 else w


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in _STOP}


def _mentions(p: dict, query: str) -> bool:
    """The shopper's words appear in the product (name, brand, kind, tags or description)."""
    text = " ".join([p.get("name") or "", p.get("brand") or "", p.get("description") or "",
                     " ".join(p.get("tags") or []), (p.get("category") or "").replace("_", " "),
                     (p.get("subcategory") or "").replace("_", " "), p.get("department") or ""])
    return bool(_words(query) & _words(text))


def _card(p: dict, avail: dict, size: Optional[str], color: Optional[str]) -> dict:
    """One recommendation: what a shopper needs, nothing internal."""
    colours = [c["name"] for c in avail.get("colors", []) if c.get("available")]
    sizes = [s["size"] for s in avail.get("sizes", []) if s.get("available")]
    photo = next((c.get("image_url") for c in p.get("colors", []) if color and _same(c.get("name"), color)),
                 p.get("image_url"))
    return {
        "product_id": p["product_id"], "name": p["name"], "brand": p.get("brand"),
        "price": p.get("price"), "currency": p.get("currency", "USD"),
        "rating": p.get("rating"), "review_count": p.get("review_count"),
        "description": p.get("description"), "highlights": p.get("tags", []),
        "size_requested": size, "available_colors": colours,       # in the requested size, if one was given
        "available_sizes": sizes if avail.get("has_sizes") else [], "option_label": avail.get("option_label"),
        "delivery_days": p.get("delivery_days"),
        "url": f"{_site()}/p/{p['product_id']}", "image_url": _abs(photo),
    }


def find_products(query: str, category: Optional[str] = None, size: Optional[str] = None,
                  color: Optional[str] = None, max_price: Optional[float] = None,
                  gender: Optional[str] = None, brand: Optional[str] = None, limit: int = 3) -> dict:
    """The search behind search_shopsphere: {found, products, notes, next_step, ...}."""
    limit = max(1, min(int(limit or 3), 5))
    blocked = parse.not_sold(query)
    if blocked:                                            # groceries, furniture, appliances…: never sold here
        return {"store": "ShopSphere", "found": False, "query": query, "size": size, "products": [],
                "notes": [f"ShopSphere doesn't sell {blocked[0]} ({blocked[1]})."],
                "next_step": "ShopSphere has no match. Say so in one line, then search the web right away "
                             "(don't ask first) and label those results as other stores, not ShopSphere."}
    # Same reading of the request as the Talkshop panel: kind of product, brand, budget, size.
    intent = parse.search_intent(query) or {}
    cat = (category or "").strip().lower()
    cat = cat if cat in _CATEGORIES else intent.get("category")
    brand = brand or intent.get("brand")
    max_price = max_price if max_price is not None else intent.get("max_price")
    gender = gender or intent.get("gender") or parse.gender_of(query)
    size = size or parse.asked_size(query)
    res = talkshop_tools.search_products(query=query, category=cat, brand=brand, max_price=max_price,
                                         gender=gender, color=color, limit=12)
    if not color:                                          # a colour named in the request ("black sneakers")
        named = {c["name"].lower(): c["name"] for p in res["products"] for c in p.get("colors", [])}
        color = next((named[w] for w in re.findall(r"[a-z]+", query.lower()) if w in named), None)
        if color:
            res = talkshop_tools.search_products(query=query, category=cat, brand=brand, max_price=max_price,
                                                 gender=gender, color=color, limit=12)
    picks = []
    for p in res["products"]:
        if not cat and not _mentions(p, query):
            continue                                       # no padding with unrelated products
        avail = talkshop_tools.check_variant(p["product_id"], size=size)
        if size and avail.get("has_sizes"):
            if not any(_same(s["size"], size) and s.get("available") for s in avail.get("sizes", [])):
                continue                                   # not in stock in that size
        if color and not any(_same(c["name"], color) and c.get("available") for c in avail.get("colors", [])):
            if "color" not in res["relaxed"]:
                continue
        if not any(c.get("available") for c in avail.get("colors", [])) and avail.get("colors"):
            continue                                       # nothing left to buy
        picks.append(_card(p, avail, size, color))
        if len(picks) == limit:
            break
    notes = []
    if res["brand_missing"]:
        notes.append(f"ShopSphere doesn't carry {brand}; these are similar products ShopSphere does carry.")
    if "color" in res["relaxed"]:
        notes.append(f"No {color} option matched, so other colours are shown.")
    if "max_price" in res["relaxed"]:
        notes.append(f"Nothing matched under ${max_price:.0f}, so the closest options are shown.")
    found = bool(picks)
    return {
        "store": "ShopSphere", "found": found, "query": query, "size": size, "products": picks,
        "notes": notes,
        "next_step": ("Recommend these ShopSphere products only." if found else
                      "ShopSphere has no match. Say so in one line, then search the web right away "
                      "(don't ask first) and label those results as other stores, not ShopSphere."),
    }


@mcp.tool(annotations=READ_ONLY)
def get_shopsphere_product(product_id: str, size: Optional[str] = None) -> dict:
    """Details of one ShopSphere product (from search_shopsphere): description, price, every
    size and colour with stock. Pass size to see which colours are in stock in that size."""
    p = talkshop_tools.get_product(product_id)
    if not p:
        return {"store": "ShopSphere", "found": False, "message": f"No ShopSphere product {product_id}."}
    avail = talkshop_tools.check_variant(product_id, size=size)
    return {"store": "ShopSphere", "found": True, **_card(p, avail, size, None),
            "all_colors": [{"name": c["name"], "in_stock": c.get("available")} for c in avail.get("colors", [])],
            "sizes": avail.get("sizes", [])}


# ── Product cards inside the chat (MCP Apps) ───────────────────────────────────

cards = FastMCPApp("ShopSphere")
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}


def _photo(image_url: Optional[str], px: int = 240) -> Optional[str]:
    """A small JPEG thumbnail of the product photo, embedded in the card as a data URI: cards
    render in a sandbox that may not load images from other sites, and full photos would make
    a card too big for the chat app (5 photos were ~300 KB; thumbnails are a few KB each)."""
    if image_url and image_url.startswith(_site()):
        image_url = image_url[len(_site()):]
    if not image_url or not image_url.startswith("/catalog/"):
        return None
    return _thumbnail(image_url[len("/catalog/"):], px)


@lru_cache(maxsize=512)
def _thumbnail(rel_path: str, px: int) -> Optional[str]:
    from PIL import Image as PILImage
    f = (CATALOG_IMAGE_DIR / rel_path).resolve()
    if CATALOG_IMAGE_DIR.resolve() not in f.parents or not f.is_file():
        return None
    try:
        with PILImage.open(f) as photo:
            photo = photo.convert("RGB")
            photo.thumbnail((px, px))
            buf = BytesIO()
            photo.save(buf, format="JPEG", quality=72, optimize=True)
    except OSError:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _ui(view, state: dict, data: dict) -> ToolResult:
    """A card for the shopper plus the same facts as JSON for the model."""
    shown = _prefab_to_tool_result(PrefabApp(view=view, state=state), fastmcp_app_name="ShopSphere")
    return ToolResult(content=[TextContent(type="text", text=json.dumps(data, default=str))],
                      structured_content=shown.structured_content)


def _money(x) -> str:
    return f"${float(x or 0):.2f}"


def _failed_toast(title: str = "Couldn't do that"):
    return ShowToast(title, description=ERROR, variant="error")


def _variant_label(size: Optional[str], color: Optional[str]) -> str:
    return " · ".join(x for x in (f"Size {size}" if size else None, color) if x)


def _cards_view(result: dict) -> tuple[Column, dict]:
    """Product cards for one search: photo, price, size and colour pickers, Add to cart."""
    items, size = result["products"], result.get("size")
    cart_arg = {"visitor_cart": result["visitor_cart"]} if result.get("visitor_cart") else {}
    state: dict = {}
    with Column(gap=3) as view:
        H3("ShopSphere picks" + (f" in size {size}" if size else ""))
        for note in result.get("notes", []):
            Muted(note)
        if not items:
            Muted(f"No ShopSphere match for “{result['query']}”.")
        with Grid(columns={"default": 1, "sm": 2, "md": 3}, gap=3):
            for i, it in enumerate(items):
                sizes, colours = it["available_sizes"], it["available_colors"]
                state[f"size{i}"] = size if size in sizes else (sizes[0] if sizes else "")
                state[f"color{i}"] = colours[0] if colours else ""
                state[f"added{i}"] = False
                with Card():
                    photo = _photo(it["image_url"])
                    if photo:
                        Image(src=photo, alt=it["name"], css_class="w-full h-40 object-contain bg-white")
                    with CardContent(), Column(gap=2):
                        Muted(it["brand"] or "")
                        Text(it["name"], css_class="font-medium")
                        with Row(gap=2, align="center"):
                            Text(_money(it["price"]), css_class="font-semibold")
                            if it.get("rating"):
                                Muted(f"★ {it['rating']} ({it.get('review_count') or 0})")
                        if sizes:
                            with Select(name=f"size{i}", value=state[f"size{i}"], size="sm"):
                                for s in sizes:
                                    SelectOption(value=s, label=f"{it.get('option_label') or 'Size'} {s}")
                        if colours:
                            with Select(name=f"color{i}", value=state[f"color{i}"], size="sm"):
                                for c in colours:
                                    SelectOption(value=c, label=c)
                    with CardFooter(), Column(gap=2):
                        with Row(gap=2):
                            Button("Add to cart", size="sm", on_click=CallTool("add_to_cart", arguments={
                                "product_id": it["product_id"],
                                "size": getattr(STATE, f"size{i}") if sizes else None,
                                "color": getattr(STATE, f"color{i}") if colours else None, "quantity": 1,
                                **cart_arg},
                                onSuccess=[SetState(f"added{i}", True),
                                           ShowToast("Added to your ShopSphere cart", variant="success")],
                                onError=_failed_toast("Couldn't add it")))
                            Button("Details", variant="outline", size="sm",
                                   on_click=SendMessage(f"Tell me more about the {it['name']}."))
                        with If(getattr(STATE, f"added{i}")):
                            Button("View cart & checkout", variant="secondary", size="sm",
                                   on_click=SendMessage("Show my ShopSphere cart."
                                                        + (f" (visitor_cart {cart_arg['visitor_cart']})"
                                                           if cart_arg else "")))
    return view, state


@cards.ui(annotations=READ_ONLY)
def search_shopsphere(query: str, category: Optional[str] = None, size: Optional[str] = None,
                      color: Optional[str] = None, max_price: Optional[float] = None,
                      gender: Optional[str] = None, brand: Optional[str] = None, limit: int = 3,
                      visitor_cart: Optional[str] = None) -> ToolResult:
    """Find products to buy at ShopSphere, the shopper's store: shoes, running shoes, sneakers,
    boots, clothing (dresses, jeans, jackets, sweaters, t-shirts), bags, sunglasses, watches,
    smartwatches, earbuds, headphones, speakers, phones and laptops. Use this FIRST for ANY
    request to find, recommend, compare or buy a product, before web search. Shows the
    shopper product cards (photos, prices, size/colour pickers, Add to cart) and returns the
    same products to you as JSON, to recommend below the cards.

    Args:
        query: what the shopper wants, in their words, e.g. "running shoes", "wireless earbuds".
        category: optional: shoes, clothing, electronics, accessories, earbuds, headphones,
            speakers, smartwatches or fitness_trackers.
        size: optional size the shopper needs, e.g. "10" for shoes or "M" for clothing.
            Only products with that size in stock are returned.
        color: optional colour, e.g. "black".
        max_price: optional budget in USD.
        gender: optional: men, women or unisex.
        brand: optional brand name.
        limit: how many recommendations (1-5, default 3).
        visitor_cart: the visitor_cart id from an earlier result in this chat, if any.

    Returns found=true with ShopSphere products in stock, or found=false: then search the web
    yourself right away (don't ask) and label those results as other stores, not ShopSphere.
    """
    result = find_products(query, category=category, size=size, color=color, max_price=max_price,
                           gender=gender, brand=brand, limit=limit)
    _with_cart(result, visitor_cart)
    view, state = _cards_view(result)
    return _ui(view, state, result)


@cards.ui(annotations=READ_ONLY)
def show_shopsphere_cards(product_ids: list[str], size: Optional[str] = None,
                          color: Optional[str] = None, visitor_cart: Optional[str] = None) -> ToolResult:
    """Show specific ShopSphere products as cards (photo, price, size/colour pickers, Add to
    cart), e.g. products from an earlier search_shopsphere, to show again or compare. Pass their
    product_ids (up to 5) and the size/colour the shopper wants."""
    # Kept under this name: chat apps that saved an earlier tool list call it, and its card
    # address (ui://prefab/tool/<hash of the name>/renderer.html) must stay the same.
    picks = []
    for pid in list(dict.fromkeys(product_ids or []))[:5]:
        p = talkshop_tools.get_product(pid)
        if p:
            picks.append(_card(p, talkshop_tools.check_variant(pid, size=size), size, color))
    result = {"store": "ShopSphere", "found": bool(picks), "query": ", ".join(product_ids or []), "size": size,
              "products": picks, "notes": [], "next_step": "Recommend these ShopSphere products only."}
    _with_cart(result, visitor_cart)
    view, state = _cards_view(result)
    return _ui(view, state, result)


# ── Account, cart, checkout and payment ─────────────────────────────────────────

def _with_cart(result: dict, visitor_cart: Optional[str]) -> None:
    """Before sign-in, a search's cards add to this chat's visitor cart (new, or the one passed)."""
    if result.get("products") and optional_customer() is None:
        vid = read_visitor_cart(visitor_cart) or f"VIS{secrets.token_hex(4).upper()}"
        result["visitor_cart"] = visitor_cart_id(vid)
        result["next_step"] = (result.get("next_step") or "") + (
            " The shopper isn't signed in: pass visitor_cart to later search_shopsphere, add_to_cart, "
            "show_cart and checkout calls in this chat. Sign-in is only needed at checkout.")


def _shopper(visitor_cart: Optional[str]) -> tuple[str, Optional[str]]:
    """(user id, visitor_cart id) for a cart call. Signed in: the customer's cart, and a visitor
    cart from before sign-in is moved into it. Not signed in: this chat's visitor cart."""
    vid = read_visitor_cart(visitor_cart)
    user = optional_customer()
    if user:
        if vid:
            account.adopt_visitor(vid, user.user_id)
        return user.user_id, None
    vid = vid or f"VIS{secrets.token_hex(4).upper()}"
    with Session(get_engine()) as db:
        if not db.query(User).filter_by(user_id=vid).first():
            account.new_visitor(db, vid)
            db.commit()
    return vid, visitor_cart_id(vid)



@mcp.tool(annotations=READ_ONLY)
def my_shopsphere_account() -> dict:
    """The signed-in ShopSphere customer: name, email, points and saved card. Call this when
    the shopper asks to sign in or who they are; it asks them to sign in if they haven't."""
    user = current_customer()
    with Session(get_engine()) as db:
        saved = profile.list_cards(db, user.user_id)
        default = next((c for c in saved if c.is_default), saved[0] if saved else None)
        points = checkout_service.points_balance(db, user.user_id)
    return {"signed_in": True, "name": user.name, "email": user.email, "points": points,
            "chase_benefits": talkshop_tools.has_chase_benefits(user.user_id),
            "saved_card": f"{default.brand} •••• {default.last4}" if default else None,
            "cart_items": talkshop_tools.get_cart(user.user_id)["item_count"]}


def _resolve_sku(product_id: str, size: Optional[str], color: Optional[str]) -> tuple[dict, str, Optional[str]]:
    p = talkshop_tools.get_product(product_id)
    if not p:
        raise ToolError(f"No ShopSphere product {product_id}.")
    avail = talkshop_tools.check_variant(product_id, size=size or None)
    colours = [c["name"] for c in avail.get("colors", []) if c.get("available")]
    if avail.get("has_sizes") and not size:
        sizes = [s["size"] for s in avail.get("sizes", []) if s.get("available")]
        raise ToolError(f"Which {(avail.get('option_label') or 'size').lower()}? In stock: {', '.join(sizes)}.")
    if not color:
        if len(colours) != 1:
            raise ToolError(f"Which colour? In stock: {', '.join(colours) or 'none'}.")
        color = colours[0]
    sel = talkshop_tools.check_variant(product_id, size=size or None, color=color).get("selected")
    if not sel or not sel.get("available"):
        raise ToolError(f"{p['name']} isn't in stock in {_variant_label(size, color)}. "
                        f"Colours in stock: {', '.join(colours) or 'none'}.")
    return p, sel["sku"], color


@cards.tool(model=True)
def add_to_cart(product_id: str, size: Optional[str] = None, color: Optional[str] = None, quantity: int = 1,
                visitor_cart: Optional[str] = None) -> dict:
    """Add a ShopSphere product to the shopper's ShopSphere cart (no sign-in needed; once signed
    in, the same cart as the website). Needs the size (if the product has sizes) and colour, and
    the visitor_cart id from earlier results in this chat if there is one. Nothing is charged."""
    p, sku, color = _resolve_sku(product_id, size, color)
    uid, cart_id = _shopper(visitor_cart)
    try:
        cart, line_id = talkshop_tools.add_to_cart(uid, sku, max(1, min(int(quantity or 1), 10)))
    except CartError as exc:
        raise ToolError(exc.message)
    out = {"added": p["name"], "sku": sku, "size": size, "color": color, "line_id": line_id,
           "cart_items": cart["item_count"], "cart_subtotal": cart["subtotal"],
           "next_step": "Offer show_cart or checkout. Nothing has been charged."}
    if cart_id:
        out.update(visitor_cart=cart_id, next_step=out["next_step"] + " Pass visitor_cart to show_cart, "
                   "checkout and guest_checkout. At checkout the shopper signs in, creates an account (the cart "
                   "comes along) or checks out as a guest.")
    return out


@cards.tool()
def update_cart_line(line_id: str, quantity: int, visitor_cart: Optional[str] = None) -> dict:
    """App-only (cart card buttons): set a cart line's quantity; 0 removes it."""
    uid, _ = _shopper(visitor_cart)
    with Session(get_engine()) as db:
        try:
            cart = cart_service.set_quantity(db, uid, line_id, int(quantity))
        except CartError as exc:
            raise ToolError(exc.message)
    return {"cart_items": cart["item_count"], "cart_subtotal": cart["subtotal"]}


def _cart_view(cart: dict, cart_id: Optional[str] = None) -> tuple[Column, dict]:
    extra = {"visitor_cart": cart_id} if cart_id else {}
    state: dict = {}
    with Column(gap=3) as view:
        H3("Your ShopSphere cart")
        if not cart["lines"]:
            Muted("Your cart is empty. Ask me to find something!")
        for i, ln in enumerate(cart["lines"]):
            q, gone = f"q{i}", f"gone{i}"
            state[q], state[gone] = ln["quantity"], False
            with If(~getattr(STATE, gone)):
                with Card():
                    with CardContent(), Row(gap=3, align="center"):
                        photo = _photo(ln.get("image_url"), 96)
                        if photo:
                            Image(src=photo, alt=ln["name"], css_class="w-16 h-16 object-contain bg-white")
                        with Column(gap=1, css_class="flex-1"):
                            Text(ln["name"], css_class="font-medium")
                            Muted(" · ".join(x for x in (_variant_label(ln.get("size"), ln.get("color")),
                                                             _money(ln["unit_price"]) + " each") if x))
                            if not ln.get("in_stock", True):
                                Muted("Out of stock: remove it to check out.")
                        with Row(gap=2, align="center"):
                            Button("−", variant="outline", size="sm", on_click=CallTool(
                                "update_cart_line", arguments={"line_id": ln["line_id"],
                                                               "quantity": getattr(STATE, q) - 1, **extra},
                                onSuccess=SetState(q, getattr(STATE, q) - 1), onError=_failed_toast()))
                            Text(f"{getattr(STATE, q)}", css_class="font-medium")
                            Button("+", variant="outline", size="sm", on_click=CallTool(
                                "update_cart_line", arguments={"line_id": ln["line_id"],
                                                               "quantity": getattr(STATE, q) + 1, **extra},
                                onSuccess=SetState(q, getattr(STATE, q) + 1), onError=_failed_toast()))
                            Button("Remove", variant="ghost", size="sm", on_click=CallTool(
                                "update_cart_line", arguments={"line_id": ln["line_id"], "quantity": 0, **extra},
                                onSuccess=SetState(gone, True), onError=_failed_toast()))
        if cart["lines"]:
            Muted(f"Subtotal when shown: {_money(cart['subtotal'])}. Offers, points, delivery and tax are "
                  "added at checkout.")
            if cart_id:
                Text("How would you like to check out?", css_class="font-medium")
                Muted("Sign in to your ShopSphere account, create one (your cart comes with you), or check "
                      "out as a guest with no account.")
                with Row(gap=2, css_class="flex-wrap"):
                    Button("Sign in & check out", on_click=SendMessage(
                        f"Sign in to ShopSphere and check out my cart (visitor_cart {cart_id})."))
                    Button("Create account & check out", variant="outline", on_click=SendMessage(
                        f"Create a ShopSphere account and check out my cart (visitor_cart {cart_id})."))
                    Button("Check out as guest", variant="secondary", on_click=SendMessage(
                        f"Check out my ShopSphere cart as a guest (visitor_cart {cart_id})."))
            else:
                Button("Checkout", on_click=SendMessage("Check out my ShopSphere cart."))
    return view, state


@cards.ui(annotations=READ_ONLY)
def show_cart(visitor_cart: Optional[str] = None) -> ToolResult:
    """Show the shopper's ShopSphere cart as a card (change quantities, remove, Checkout). No sign-in
    needed: pass the visitor_cart id from earlier results in this chat if there is one."""
    uid, cart_id = _shopper(visitor_cart)
    cart = talkshop_tools.get_cart(uid)
    view, state = _cart_view(cart, cart_id)
    data = {"items": [{k: ln.get(k) for k in ("line_id", "name", "size", "color", "quantity", "unit_price",
                                             "line_total", "in_stock")} for ln in cart["lines"]],
            "item_count": cart["item_count"], "subtotal": cart["subtotal"],
            "next_step": "To buy, call checkout (optionally with line_ids)"
                         + (", passing visitor_cart: the shopper signs in or creates an account then. Or, if "
                            "they choose to check out as a guest, call guest_checkout with visitor_cart." if cart_id
                            else ".")}
    if cart_id:
        data["visitor_cart"] = cart_id
    return _ui(view, state, data)


def _checkout_state(co: dict) -> dict:
    """What the checkout card shows, as values the card can refresh in place after a change."""
    path = co.get("payment_path") or {}
    route = path.get("route")
    dl = co.get("delivery") or {}
    chosen = next((o for o in dl.get("options", []) if o["method"] == dl.get("method")), {})
    points = co.get("points") or {}
    card = (path.get("card") or {}).get("display") or path.get("label") or "no saved card"
    return {
        "checkout_id": co["checkout_id"], "version": co["checkout_version"], "total": co["total"],
        "currency": co.get("currency") or "USD", "total_text": _money(co["total"]),
        "subtotal_text": _money(co["subtotal"]),
        "has_offer": bool(co.get("offer_discount")),
        "offer_label": f"Chase offer: {(co.get('offer') or {}).get('title', 'offer')}",
        "offer_text": "−" + _money(co.get("offer_discount")),
        "has_points": bool(co.get("points_discount")),
        "points_label": f"Points ({points.get('used', 0)})", "points_text": "−" + _money(co.get("points_discount")),
        "delivery_label": f"Delivery: {chosen.get('label', 'Standard')}, arrives {dl.get('date')}",
        "shipping_text": _money(co.get("shipping")), "arrives": dl.get("date"),
        "tax_text": _money(co.get("tax")),
        "ship_to": f"Ship to: {(co.get('address') or {}).get('display') or 'no address yet'}",
        "pay_with_text": "Pay with: " + ("ShopSphere Wallet" if route == "wallet" else
                                         "a new card on the secure payment page" if route == "provider_handoff"
                                         else card)
                         + (" · Visa agent payment (VIC-inspired, simulated)" if route == "vic" else ""),
        "new_card": route == "provider_handoff",
        "earn_text": f"You'll earn {co.get('earn_points') or 0} points with this order.",
        "payable": bool(co.get("ready")) and route in ("saved_card", "vic", "wallet", "provider_handoff"),
        "problem": " ".join(i.get("message") or f"{i.get('name') or 'Item'}: {i.get('code')}"
                            for i in co.get("issues") or []),
    }


_REFRESHED = ("version", "total", "total_text", "subtotal_text", "has_offer", "offer_label", "offer_text",
              "has_points", "points_label", "points_text", "delivery_label", "shipping_text", "arrives", "tax_text",
              "ship_to", "pay_with_text", "new_card", "earn_text", "payable", "problem")


def _checkout_view(co: dict) -> tuple[Column, dict]:
    """The review card: items, totals and choices (address, card, delivery, points) that update
    the same card in place; then Approve & pay -> simulated passkey -> order confirmed."""
    st = _checkout_state(co)
    pts = co.get("points") or {}
    dl = co.get("delivery") or {}
    state = {**st, "confirming": False, "paid": False, "order_ref": "", "handing_off": False,
             "hosted_url": "", "session_id": "",
             "address_id": (co.get("address") or {}).get("address_id") or "",
             "pay_id": co.get("pay_with") if co.get("pay_with") in ("wallet", "provider") else
                       (co.get("payment_method") or {}).get("payment_method_id") or "",
             "delivery": dl.get("method") or "standard",
             "use_points": "yes" if co.get("points_discount") else "no"}
    S = STATE
    with Column(gap=3) as view:
        H3("Review your ShopSphere order")
        with Card():
            with CardContent(), Column(gap=2):
                for ln in co["lines"]:
                    with Row(gap=2, css_class="justify-between"):
                        Text(f"{ln['name']} ({_variant_label(ln.get('size'), ln.get('color'))}) × {ln['quantity']}")
                        Text(_money(ln["unit_price"] * ln["quantity"]))
                Separator()
                with Row(gap=2, css_class="justify-between"):
                    Muted("Subtotal")
                    Muted(f"{S.subtotal_text}")
                with If(S.has_offer):
                    with Row(gap=2, css_class="justify-between"):
                        Muted(f"{S.offer_label}")
                        Muted(f"{S.offer_text}")
                with If(S.has_points):
                    with Row(gap=2, css_class="justify-between"):
                        Muted(f"{S.points_label}")
                        Muted(f"{S.points_text}")
                with Row(gap=2, css_class="justify-between"):
                    Muted(f"{S.delivery_label}")
                    Muted(f"{S.shipping_text}")
                with Row(gap=2, css_class="justify-between"):
                    Muted("Tax")
                    Muted(f"{S.tax_text}")
                with Row(gap=2, css_class="justify-between"):
                    Text("Total", css_class="font-semibold")
                    Text(f"{S.total_text}", css_class="font-semibold")
                Muted(f"{S.ship_to}")
                Muted(f"{S.pay_with_text}")
                Muted(f"{S.earn_text}")
                with If(~S.paid):
                    Separator()
                    Text("Change your order", css_class="font-medium")
                    with Grid(columns={"default": 1, "sm": 2}, gap=2):
                        with Select(name="address_id", value=state["address_id"], size="sm"):
                            for a in co.get("saved_addresses") or []:
                                SelectOption(value=a["address_id"], label=f"Ship to {a.get('label') or 'address'}: {a['display']}")
                        with Select(name="pay_id", value=state["pay_id"], size="sm"):
                            for c in co.get("saved_payment_methods") or []:
                                SelectOption(value=c["payment_method_id"],
                                             label=f"Pay with {c.get('issuer_display') + ' ' if c.get('issuer_display') else ''}{c['display']}")
                            if (co.get("wallet") or {}).get("enough") and co.get("saved_payment_methods"):
                                SelectOption(value="wallet", label=f"Pay with ShopSphere Wallet ({_money(co['wallet'].get('balance'))})")
                            if co.get("provider_option"):
                                SelectOption(value="provider", label="Pay with a new card (secure payment page)")
                        with Select(name="delivery", value=state["delivery"], size="sm"):
                            for o in dl.get("options") or []:
                                SelectOption(value=o["method"], label=f"{o['label']} delivery, arrives {o['date']}"
                                             + (f" (+{_money(o['price'])})" if o.get("price") else " (free)"))
                        if pts.get("max_usable"):
                            with Select(name="use_points", value=state["use_points"], size="sm"):
                                SelectOption(value="no", label="Don't use points")
                                SelectOption(value="yes", label=f"Use {pts['max_usable']} points "
                                                                f"(−{_money(pts['max_usable'] * pts.get('value_per_point', 0.01))})")
                    Button("Update order", variant="outline", size="sm", on_click=CallTool(
                        "update_checkout_choices", arguments={
                            "checkout_id": co["checkout_id"], "address_id": S.address_id, "pay_with": S.pay_id,
                            "delivery": S.delivery, "use_points": S.use_points},
                        onSuccess=[*(SetState(k, getattr(RESULT, k)) for k in _REFRESHED),
                                   ShowToast("Order updated", variant="success")],
                        onError=_failed_toast("Couldn't update the order")))
                    Muted("New card? Choose \"a new card\" above. You type it only on the secure payment page "
                          "(and can save it there), never in the chat. New addresses: your ShopSphere account.")
            with CardFooter(), Column(gap=2):
                with If(~S.payable):
                    Muted(f"This order can't be paid here yet. {S.problem} Fix it in your cart or choose another "
                          "card, then update the order.")
                with If(S.payable):
                    with If(~S.paid), If(S.new_card):
                        with If(~S.handing_off):
                            Button(f"Approve & pay {S.total_text}", on_click=CallTool(
                                "start_new_card_payment", arguments={
                                    "checkout_id": co["checkout_id"], "checkout_version": S.version,
                                    "final_amount": S.total, "currency": state["currency"]},
                                onSuccess=[SetState("hosted_url", RESULT.hosted_url),
                                           SetState("session_id", RESULT.payment_session_id),
                                           SetState("handing_off", True), OpenLink(RESULT.hosted_url)],
                                onError=_failed_toast("Couldn't open the secure payment page")))
                            Muted("Next you enter your card on the secure payment page (simulated provider, test "
                                  "cards only). Claude never sees it. Nothing is charged until you finish there.")
                        with If(S.handing_off):
                            Text("Finish on the secure payment page", css_class="font-medium")
                            Muted("It opened in a new browser tab. Enter the card there (tick Save to keep it "
                                  "for next time), then come back here.")
                            with Row(gap=2):
                                Button("Open secure payment page", variant="outline", on_click=OpenLink(S.hosted_url))
                                Button("I've finished paying", variant="success", on_click=CallTool(
                                    "finish_new_card_payment", arguments={
                                        "checkout_id": co["checkout_id"], "payment_session_id": S.session_id},
                                    onSuccess=[SetState("paid", True), SetState("order_ref", RESULT.order_id),
                                               ShowToast("Payment approved", variant="success")],
                                    onError=_failed_toast("Not finished yet")))
                    with If(~S.paid), If(~S.new_card):
                        with If(~S.confirming):
                            Button(f"Approve & pay {S.total_text}", on_click=SetState("confirming", True))
                            Muted("Nothing is charged until you approve. Claude can't press this button.")
                        with If(S.confirming):
                            Text("Confirm it's you", css_class="font-medium")
                            Muted("Simulated passkey (demo, not a real passkey; your card issuer isn't contacted).")
                            with Row(gap=2):
                                Button("Confirm with passkey", variant="success", on_click=CallTool(
                                    "approve_and_pay", arguments={
                                        "checkout_id": co["checkout_id"], "checkout_version": S.version,
                                        "final_amount": S.total, "currency": state["currency"]},
                                    onSuccess=[SetState("paid", True), SetState("order_ref", RESULT.order_id),
                                               ShowToast("Payment approved", variant="success")],
                                    onError=[SetState("confirming", False), _failed_toast("Payment not completed")]))
                                Button("Cancel", variant="outline", on_click=SetState("confirming", False))
                    with If(S.paid):
                        Text(f"✓ Order {S.order_ref} confirmed", css_class="font-semibold")
                        Muted(f"Arrives {S.arrives}. A confirmation email is on its way.")
                        Button("Track my order", variant="outline", size="sm",
                               on_click=SendMessage("Show my ShopSphere orders."))
    return view, state


def _apply_choices(user_id: str, co: dict, address_id: Optional[str] = None, pay_with: Optional[str] = None,
                   delivery: Optional[str] = None, use_points=None) -> dict:
    """Change the address, card / wallet, delivery or points of an open checkout."""
    changes: dict = {}
    if address_id and address_id != (co.get("address") or {}).get("address_id"):
        changes["address_id"] = address_id
    if pay_with in ("wallet", "provider"):
        if co.get("pay_with") != pay_with:
            changes["pay_with"] = pay_with
    elif pay_with:
        if pay_with != (co.get("payment_method") or {}).get("payment_method_id") or co.get("pay_with") != "card":
            changes.update(payment_method_id=pay_with, pay_with="card")
    if delivery in ("standard", "express") and delivery != (co.get("delivery") or {}).get("method"):
        changes["delivery_method"] = delivery
    if use_points is not None:
        want = str(use_points).lower() in ("yes", "true", "1")
        changes["points_used"] = (co.get("points") or {}).get("max_usable", 0) if want else 0
    if not changes:
        return co
    try:
        return talkshop_tools.update_checkout(user_id, co["checkout_id"], **changes)
    except CheckoutError as exc:
        raise ToolError(exc.message)


@cards.ui(annotations=WRITE)
def checkout(line_ids: Optional[list[str]] = None, delivery: Optional[str] = None, use_points: bool = False,
             address_id: Optional[str] = None, payment_method_id: Optional[str] = None,
             visitor_cart: Optional[str] = None) -> ToolResult:
    """Prepare checkout for the signed-in shopper's cart and show the review card: items, Chase
    offers, points, delivery, tax, total, address and saved card. On the card the shopper can
    change the address, card (or ShopSphere Wallet), delivery and points, then tap Approve & pay.
    Only the shopper can approve; you can't pay. Nothing is charged by this call.

    Args:
        line_ids: optional cart line ids to buy (from show_cart); default: the whole cart.
        delivery: optional "standard" or "express".
        use_points: true to spend the shopper's points on this order.
        address_id: optional saved address id (from saved_addresses); default: their default address.
        payment_method_id: optional saved card id, "wallet", or "provider" (a new card on the secure
            payment page); default: their default card, or "provider" when they have none.
        visitor_cart: the visitor_cart id from before sign-in, if any: that cart comes along.
    """
    user = current_customer()
    vid = read_visitor_cart(visitor_cart)
    moved = account.adopt_visitor(vid, user.user_id) if vid else {}
    if line_ids:
        line_ids = [moved.get(x, x) for x in line_ids]
    cart = talkshop_tools.get_cart(user.user_id)
    ids = [ln["line_id"] for ln in cart["lines"] if not line_ids or ln["line_id"] in line_ids]
    if not ids:
        raise ToolError("The ShopSphere cart is empty. Add something first.")
    try:
        co = talkshop_tools.create_checkout(user.user_id, ids)
    except CheckoutError as exc:
        raise ToolError(exc.message)
    if not payment_method_id and not co.get("saved_payment_methods") and co.get("provider_option"):
        payment_method_id = "provider"             # a new shopper: pay with a new card on the secure page
    co = _apply_choices(user.user_id, co, address_id, payment_method_id, delivery, True if use_points else None)
    view, state = _checkout_view(co)
    data = {k: co.get(k) for k in ("checkout_id", "checkout_version", "subtotal", "offer_discount", "points_discount",
                                   "shipping", "tax", "total", "currency", "earn_points", "ready", "issues")}
    data.update({"items": [{"name": ln["name"], "size": ln.get("size"), "color": ln.get("color"),
                            "quantity": ln["quantity"]} for ln in co["lines"]],
                 "delivery": (co.get("delivery") or {}).get("method"), "arrives": (co.get("delivery") or {}).get("date"),
                 "ship_to": (co.get("address") or {}).get("display"),
                 "pay_with": state["pay_with_text"],
                 "saved_addresses": [{"address_id": a["address_id"], "label": a.get("label"), "display": a["display"]}
                                     for a in co.get("saved_addresses") or []],
                 "saved_payment_methods": [{"payment_method_id": c["payment_method_id"], "display": c["display"],
                                            "issuer": c.get("issuer_display")} for c in co.get("saved_payment_methods") or []],
                 "next_step": "The shopper can change address, card, delivery or points on the card (or ask you: call "
                              "update_checkout_choices), then approves on the card (Approve & pay, then the simulated "
                              "passkey). Never say the order is paid unless they tell you it went through."})
    return _ui(view, state, data)


@cards.tool(model=True)
def update_checkout_choices(checkout_id: str, address_id: Optional[str] = None, pay_with: Optional[str] = None,
                            delivery: Optional[str] = None, use_points: Optional[str] = None) -> dict:
    """Change an open ShopSphere checkout: shipping address (address_id), payment (a saved card's
    payment_method_id, "wallet", or "provider" = a new card on the secure payment page), delivery ("standard"/"express") and points ("yes"/"no").
    Totals and Chase offers are recalculated (Chase offers need a Chase card). Nothing is charged."""
    user = current_customer()
    try:
        co = talkshop_tools.get_checkout(user.user_id, checkout_id)
    except CheckoutError as exc:
        raise ToolError(exc.message)
    if co.get("status") != "open":
        raise ToolError("This order is already closed. Ask for a new checkout.")
    co = _apply_choices(user.user_id, co, address_id, pay_with, delivery, use_points)
    return _checkout_state(co)


@cards.tool()
async def approve_and_pay(checkout_id: str, checkout_version: int, final_amount: float, currency: str = "USD") -> dict:
    """App-only: the shopper pressed Confirm with passkey on the checkout card. Runs the same
    approval and payment as the website: scoped consent for this exact order, the simulated
    passkey, then the simulated gateway. The model can't call this."""
    user = current_customer()
    uid = user.user_id
    scope = {"action": "PAY", "checkout_version": int(checkout_version), "final_amount": float(final_amount),
             "currency": currency}
    try:
        with Session(get_engine()) as db:
            co = checkout_service.get_checkout(db, uid, checkout_id)
            route = payment_routing.route(db, co).get("route")
            approval_id = instruction_id = None
            if route == "saved_card":
                started = card_approval.start(db, uid, checkout_id)
                approval_id = started["approval"]["approval_id"]
                card_approval.verify(db, uid, checkout_id, approval_id, started["passkey"]["nonce"], True)
            elif route == "vic":
                instr, pk = vic_instructions.create(db, uid, checkout_id, "button")
                instruction_id = instr.instruction_id
                vic_instructions.authenticate(db, uid, instruction_id, pk["challenge_id"], pk["nonce"], True)
            elif route != "wallet":
                raise ToolError("Choose \"a new card\" on the checkout card to pay on the secure payment page."
                                if route in ("none", "provider_handoff") else
                                "Set up Visa agent payments on ShopSphere first, then try again.")
        res = await payment_service.confirm(user, checkout_id, consent=True, consent_mode="button",
                                            instruction_id=instruction_id, approval_id=approval_id, **scope)
    except (CheckoutError, VicError) as exc:
        raise ToolError(exc.message)
    if res.get("status") != "authorized":
        raise ToolError(res.get("message") or f"Payment {res.get('status')}. Nothing was charged.")
    order = res.get("order") or {}
    return {"status": "authorized", "order_id": order.get("order_id"), "total": order.get("total"),
            "arrives": order.get("delivery_date"), "email": res.get("confirmation_email")}


@cards.tool()
async def start_new_card_payment(checkout_id: str, checkout_version: int, final_amount: float,
                                 currency: str = "USD") -> dict:
    """App-only: the shopper tapped Approve & pay for a new card. Records their scoped consent for
    this exact order and opens a session on the (simulated) secure payment provider's page, where
    the card is typed. Returns that page's address. The model can't call this."""
    user = current_customer()
    with Session(get_engine()) as db:
        try:
            co = checkout_service.get_checkout(db, user.user_id, checkout_id)
        except CheckoutError as exc:
            raise ToolError(exc.message)
        if co.pay_with != "provider":
            raise ToolError("Choose \"a new card\" on the checkout card first.")
    return await _open_secure_page(user, checkout_id, checkout_version, final_amount, currency)


async def _open_secure_page(user, checkout_id: str, checkout_version: int, final_amount: float, currency: str,
                            guest: bool = False) -> dict:
    """The shopper's Approve & pay for a new card: their consent scoped to this exact order
    (version, amount, currency), then a session on the secure payment provider's page. Tapped
    again while a page is open: the same page if it's still waiting, otherwise (it expired, was
    cancelled or declined) a fresh one for the same approved order. Nothing is charged here."""
    scope = {"action": "PAY", "checkout_version": int(checkout_version), "final_amount": float(final_amount),
             "currency": currency}
    try:
        try:
            res = await payment_service.confirm(user, checkout_id, consent=True, consent_mode="button",
                                                guest=guest, **scope)
        except CheckoutError as exc:
            if exc.code != "PAYMENT_IN_PROGRESS" or not exc.detail.get("payment_session_id"):
                raise
            now = await payment_service.complete_provider(user, checkout_id, exc.detail["payment_session_id"],
                                                          guest=guest)
            if now.get("status") == "pending":                         # still open: reuse that page
                return {"status": "provider_handoff", "hosted_url": now["hosted_url"],
                        "payment_session_id": now["payment_session_id"]}
            if now.get("status") == "authorized":
                raise ToolError(f"This order is already paid: {(now.get('order') or {}).get('order_id')}. "
                                "Tap \"I've finished paying\".")
            res = await payment_service.confirm(user, checkout_id, consent=True, consent_mode="button",
                                                guest=guest, **scope)
    except CheckoutError as exc:
        raise ToolError(exc.message)
    if res.get("status") != "provider_handoff":
        raise ToolError(res.get("message") or f"Payment {res.get('status')}. Nothing was charged.")
    return {"status": "provider_handoff", "hosted_url": res["hosted_url"],
            "payment_session_id": res["payment_session_id"], "expires_at": res.get("expires_at")}


@cards.tool(model=True)
async def finish_new_card_payment(checkout_id: str, payment_session_id: Optional[str] = None) -> dict:
    """After the shopper finished on the secure payment page: ShopSphere asks the payment provider
    itself for the result and, if the card was verified, places the order (once). Call it when the
    shopper says they finished paying there. Nothing is charged unless they completed that page."""
    user = current_customer()
    if not payment_session_id:
        with Session(get_engine()) as db:
            payment_session_id = checkout_service.get_checkout(db, user.user_id, checkout_id).provider_session_id
        if not payment_session_id:
            raise ToolError("There's no open secure payment for this order. Tap Approve & pay on the checkout card.")
    try:
        res = await payment_service.complete_provider(user, checkout_id, payment_session_id[:80])
    except CheckoutError as exc:
        raise ToolError(exc.message)
    if res.get("status") == "pending":
        raise ToolError("The secure payment page isn't finished yet. Enter the card there, then tap "
                        "\"I've finished paying\" again.")
    if res.get("status") != "authorized":
        raise ToolError(res.get("message") or f"Payment {res.get('status')}. Nothing was charged.")
    order = res.get("order") or {}
    return {"status": "authorized", "order_id": order.get("order_id"), "total": order.get("total"),
            "arrives": order.get("delivery_date"), "email": res.get("confirmation_email")}


# ── Guest checkout: no account, no sign-in ─────────────────────────────────────

def _mask_email(email: Optional[str]) -> str:
    name, _, domain = (email or "").partition("@")
    return f"{name[:1]}***@{domain}" if domain else ""


def _guest_state(co: dict) -> dict:
    """The guest checkout card's values. Contact details come back masked (the card shows the
    city, state and ZIP; the email as r***@example.com), so they aren't repeated to the model."""
    dl = co.get("delivery") or {}
    chosen = next((o for o in dl.get("options", []) if o["method"] == dl.get("method")), {})
    addr, guest = co.get("address") or {}, co.get("guest") or {}
    has_details = bool(guest.get("email") and addr)
    codes = {i.get("code") for i in co.get("issues") or []}
    problems = [i for i in co.get("issues") or [] if not (i.get("code") == "NO_ADDRESS" and "NO_GUEST_DETAILS" in codes)]
    return {
        "checkout_id": co["checkout_id"], "version": co["checkout_version"], "total": co["total"],
        "currency": co.get("currency") or "USD", "total_text": _money(co["total"]),
        "subtotal_text": _money(co["subtotal"]), "tax_text": _money(co.get("tax")),
        "delivery_label": f"Delivery: {chosen.get('label', 'Standard')}, arrives {dl.get('date')}",
        "shipping_text": _money(co.get("shipping")), "arrives": dl.get("date"),
        "has_details": has_details,
        "ship_to": (f"Ship to: {addr.get('full_name') or guest.get('name') or ''}, {addr.get('city')}, "
                    f"{addr.get('state')} {addr.get('postal_code')}" if has_details
                    else "Ship to: add your details below"),
        "contact": f"Confirmation email to: {_mask_email(guest.get('email'))}" if guest.get("email") else "",
        "payable": bool(co.get("ready")) and (co.get("payment_path") or {}).get("route") == "provider_handoff",
        "problem": " ".join(i.get("message") or f"{i.get('name') or 'Item'}: {i.get('code')}" for i in problems),
    }


_GUEST_REFRESHED = ("version", "total", "total_text", "subtotal_text", "tax_text", "delivery_label", "shipping_text",
                    "arrives", "has_details", "ship_to", "contact", "payable", "problem")
_GUEST_FIELDS = {"g_name": "full_name", "g_email": "email", "g_line1": "line1", "g_line2": "line2",
                 "g_city": "city", "g_state": "state", "g_zip": "postal_code"}


def _guest_view(co: dict, token: str) -> tuple[Column, dict]:
    """Guest checkout card: items and totals, the guest's own details form (typed here, sent
    straight to ShopSphere), delivery, then Approve & pay -> secure payment page -> confirmation."""
    st = _guest_state(co)
    dl = co.get("delivery") or {}
    state = {**st, "paid": False, "order_ref": "", "handing_off": False, "hosted_url": "", "session_id": "",
             "sent_to": "", "email_note": "", "delivery": dl.get("method") or "standard",
             **{k: "" for k in _GUEST_FIELDS}}
    S = STATE
    with Column(gap=3) as view:
        H3("Guest checkout at ShopSphere")
        Muted("No account needed. Your details are used for this order only.")
        with Card():
            with CardContent(), Column(gap=2):
                for ln in co["lines"]:
                    with Row(gap=2, css_class="justify-between"):
                        Text(f"{ln['name']} ({_variant_label(ln.get('size'), ln.get('color'))}) × {ln['quantity']}")
                        Text(_money(ln["unit_price"] * ln["quantity"]))
                Separator()
                for label, key in (("Subtotal", "subtotal_text"), (None, "shipping_text"), ("Tax", "tax_text")):
                    with Row(gap=2, css_class="justify-between"):
                        Muted(label if label else f"{S.delivery_label}")
                        Muted(f"{getattr(S, key)}")
                with Row(gap=2, css_class="justify-between"):
                    Text("Total", css_class="font-semibold")
                    Text(f"{S.total_text}", css_class="font-semibold")
                Muted(f"{S.ship_to}")
                with If(S.contact):
                    Muted(f"{S.contact}")
                Muted("Pay with: a new card on the secure payment page (used once, not saved)")
                with If(~S.paid):
                    Separator()
                    Text("Your details", css_class="font-medium")
                    with Grid(columns={"default": 1, "sm": 2}, gap=2):
                        Input(name="g_name", placeholder="Full name", max_length=80, aria_label="Full name")
                        Input(name="g_email", input_type="email", placeholder="Email (for your confirmation)",
                              max_length=255, aria_label="Email")
                        Input(name="g_line1", placeholder="Street address", max_length=120, aria_label="Street address")
                        Input(name="g_line2", placeholder="Apt, suite (optional)", max_length=120,
                              aria_label="Apartment or suite")
                        Input(name="g_city", placeholder="City", max_length=80, aria_label="City")
                        Input(name="g_state", placeholder="State, e.g. TX", max_length=2, aria_label="State")
                        Input(name="g_zip", placeholder="ZIP code", max_length=10, aria_label="ZIP code")
                        with Select(name="delivery", value=state["delivery"], size="sm"):
                            for o in dl.get("options") or []:
                                SelectOption(value=o["method"], label=f"{o['label']} delivery, arrives {o['date']}"
                                             + (f" (+{_money(o['price'])})" if o.get("price") else " (free)"))
                    Button("Save details & update total", variant="outline", size="sm", on_click=CallTool(
                        "update_guest_checkout", arguments={
                            "guest_token": token, "delivery": S.delivery,
                            **{arg: getattr(S, k) for k, arg in _GUEST_FIELDS.items()}},
                        onSuccess=[*(SetState(k, getattr(RESULT, k)) for k in _GUEST_REFRESHED),
                                   ShowToast("Details saved", variant="success")],
                        onError=_failed_toast("Please check your details")))
                    Muted("Your name, email and address go straight to ShopSphere for this order; no account is "
                          "created. Card details go only on the secure payment page, never in the chat.")
            with CardFooter(), Column(gap=2):
                with If(~S.payable), If(~S.paid):
                    Muted(f"{S.problem}")
                with If(S.payable), If(~S.paid):
                    with If(~S.handing_off):
                        Button(f"Approve & pay {S.total_text}", on_click=CallTool(
                            "start_guest_payment", arguments={
                                "guest_token": token, "checkout_version": S.version, "final_amount": S.total,
                                "currency": state["currency"]},
                            onSuccess=[SetState("hosted_url", RESULT.hosted_url),
                                       SetState("session_id", RESULT.payment_session_id),
                                       SetState("handing_off", True), OpenLink(RESULT.hosted_url)],
                            onError=_failed_toast("Couldn't open the secure payment page")))
                        Muted("Next you enter your card on the secure payment page (simulated provider, test cards "
                              "only). Claude never sees it. Nothing is charged until you finish there.")
                    with If(S.handing_off):
                        Text("Finish on the secure payment page", css_class="font-medium")
                        Muted("It opened in a new browser tab. Enter the card there, then come back here.")
                        with Row(gap=2):
                            Button("Open secure payment page", variant="outline", on_click=OpenLink(S.hosted_url))
                            Button("I've finished paying", variant="success", on_click=CallTool(
                                "finish_guest_payment", arguments={"guest_token": token,
                                                                   "payment_session_id": S.session_id},
                                onSuccess=[SetState("paid", True), SetState("order_ref", RESULT.order_id),
                                           SetState("sent_to", RESULT.sent_to), SetState("arrives", RESULT.arrives),
                                           SetState("email_note", RESULT.email_note),
                                           ShowToast("Payment approved", variant="success")],
                                onError=_failed_toast("Not finished yet")))
                with If(S.paid):
                    Text("✓ Your ShopSphere order has been placed", css_class="font-semibold")
                    Text(f"Order ID: {S.order_ref}", css_class="font-medium")
                    Muted(f"Confirmation sent to: {S.sent_to}")
                    Muted(f"Estimated delivery: {S.arrives}")
                    Muted(f"{S.email_note}")
                    Muted("You can track your order using your Order ID and email address.")
                    Button("Track my order", variant="outline", size="sm",
                           on_click=SendMessage(f"Track my ShopSphere order {S.order_ref}."))
    return view, state


def _guest(token: Optional[str]):
    with Session(get_engine()) as db:
        try:
            return guest_service.load(db, token)
        except CheckoutError as exc:
            raise ToolError(exc.message)


@cards.ui(annotations=WRITE)
def guest_checkout(visitor_cart: str, line_ids: Optional[list[str]] = None,
                   delivery: Optional[str] = None) -> ToolResult:
    """Check out this chat's ShopSphere cart as a GUEST: no account and no sign-in. Use it when the
    shopper chooses "check out as guest". Shows the guest checkout card, where the shopper types
    their name, email and shipping address themselves (don't ask for them in the chat), picks
    delivery, taps Approve & pay and pays with a new card on the secure payment page. Nothing is
    charged by this call. Guests don't get points, the Wallet or Chase offers.

    Args:
        visitor_cart: the visitor_cart id from earlier results in this chat (required).
        line_ids: optional cart line ids to buy (from show_cart); default: the whole cart.
        delivery: optional "standard" or "express".
    """
    if optional_customer():
        raise ToolError("You're signed in to ShopSphere, so use checkout: your saved details apply.")
    vid = read_visitor_cart(visitor_cart)
    if not vid:
        raise ToolError("I couldn't find this chat's cart. Show the cart (or add the items) first.")
    with Session(get_engine()) as db:
        try:
            co, token = guest_service.start(db, vid, line_ids, delivery)
        except CheckoutError as exc:
            raise ToolError(exc.message)
    view, state = _guest_view(co, token)
    data = {k: co.get(k) for k in ("checkout_id", "checkout_version", "subtotal", "shipping", "tax", "total",
                                   "currency")}
    data.update({"guest_token": token, "guest": True,
                 "items": [{"name": ln["name"], "size": ln.get("size"), "color": ln.get("color"),
                            "quantity": ln["quantity"]} for ln in co["lines"]],
                 "delivery": (co.get("delivery") or {}).get("method"), "arrives": (co.get("delivery") or {}).get("date"),
                 "ready": False,
                 "next_step": "The guest types name, email and shipping address on this card (never ask for them in "
                              "the chat), then taps Approve & pay and enters a new card on the secure payment page. "
                              "If they say they finished there, call finish_guest_payment with guest_token. Never say "
                              "the order is paid unless that returns an order_id."})
    return _ui(view, state, data)


@cards.tool()
def update_guest_checkout(guest_token: str, delivery: Optional[str] = None, full_name: str = "", email: str = "",
                          line1: str = "", line2: str = "", city: str = "", state: str = "",
                          postal_code: str = "") -> dict:
    """App-only (guest checkout card): save the guest's own contact + shipping details and/or the
    delivery choice. ShopSphere validates them and recalculates the total."""
    details = {"full_name": full_name, "email": email, "line1": line1, "line2": line2 or None, "city": city,
               "state": state, "postal_code": postal_code}
    typed = any(str(v or "").strip() for v in details.values())
    with Session(get_engine()) as db:
        try:
            co = guest_service.update(db, guest_token, delivery=delivery if delivery in ("standard", "express") else None,
                                      details={k: str(v or "")[:255] if v is not None else None
                                               for k, v in details.items()} if typed else None)
        except CheckoutError as exc:
            raise ToolError(exc.message)
    return _guest_state(co)


@cards.tool()
async def start_guest_payment(guest_token: str, checkout_version: int, final_amount: float,
                              currency: str = "USD") -> dict:
    """App-only: the guest tapped Approve & pay. Records their consent for this exact order
    (version, amount, currency) and opens the secure payment provider's page, where they type a
    new card (used once, never saved). The model can't call this."""
    user, checkout_id = _guest(guest_token)
    return await _open_secure_page(user, checkout_id, checkout_version, final_amount, currency, guest=True)


@cards.tool(model=True)
async def finish_guest_payment(guest_token: str, payment_session_id: Optional[str] = None) -> dict:
    """After the guest finished on the secure payment page: ShopSphere asks the payment provider
    itself for the result and, if the card was verified, places the guest order (once) and emails
    the confirmation. Call it when the guest says they finished paying there. Nothing is charged
    unless they completed that page."""
    from backend.db.schema import Order
    user, checkout_id = _guest(guest_token)
    if not payment_session_id:
        with Session(get_engine()) as db:
            payment_session_id = checkout_service.get_checkout(db, user.user_id, checkout_id).provider_session_id
        if not payment_session_id:
            raise ToolError("There's no open secure payment for this order. Tap Approve & pay on the checkout card.")
    try:
        res = await payment_service.complete_provider(user, checkout_id, payment_session_id[:80], guest=True)
    except CheckoutError as exc:
        raise ToolError(exc.message)
    if res.get("status") == "pending":
        raise ToolError("The secure payment page isn't finished yet. Enter the card there, then tap "
                        "\"I've finished paying\" again.")
    if res.get("status") != "authorized":
        raise ToolError(res.get("message") or f"Payment {res.get('status')}. Nothing was charged.")
    order = res.get("order") or {}
    with Session(get_engine()) as db:
        row = db.query(Order).filter_by(order_id=order.get("internal_order_id")).first()
        mail = (notifications.confirmation_for(db, row) if row else None) or {}
    delivery = mail.get("delivery")
    note = {"sending": "The confirmation email is on its way.",
            "sent": "The confirmation email was sent.",
            "failed": "We couldn't send the confirmation email; your order is placed. Track it with your Order ID.",
            }.get(delivery, "Email sending isn't set up on this server, so the confirmation is kept in "
                            "ShopSphere's demo outbox (not delivered).")
    return {"status": "authorized", "order_id": order.get("order_id"), "total": order.get("total"),
            "arrives": order.get("delivery_date"), "guest": True, "sent_to": _mask_email(mail.get("to")),
            "email_delivery": delivery or "none", "email_note": note,
            "next_step": "Tell the guest their Order ID, the estimated delivery and that they can track it with "
                         "track_order (Order ID + the email they used)."}


@mcp.tool(annotations=READ_ONLY)
def track_order(order_id: str, email: str) -> dict:
    """Track any ShopSphere order (guest or customer) with its Order ID (e.g. SS-12345) and the
    email address used at checkout. No sign-in needed. Ask the shopper for both if missing. Shows
    status, shipment, estimated delivery and tracking; never the full address or card."""
    from fastmcp.server.dependencies import get_http_headers
    headers = get_http_headers(include_all=True)
    caller = (headers.get("x-forwarded-for") or "").split(",")[0].strip() or "mcp"
    order_id, email = (order_id or "")[:20], (email or "")[:255]
    if ratelimit.allow_tracking(caller, order_id):
        with Session(get_engine()) as db:
            order = order_service.find_for_tracking(db, order_id, email)
            if order is not None:
                return {"found": True, "order": order_service.tracking_view(db, order)}
        ratelimit.tracking_failed(order_id)
    return {"found": False, "message": order_service.NOT_FOUND}      # the same answer for every failure


@mcp.tool(annotations=READ_ONLY)
def my_orders(order_id: Optional[str] = None) -> dict:
    """The signed-in shopper's recent ShopSphere orders with status and tracking (or one order).
    Guests (no account) use track_order with their Order ID and email instead."""
    user = current_customer()
    if order_id:
        one = talkshop_tools.own_order(user.user_id, order_id)
        return {"orders": [one] if one else [], "found": bool(one)}
    return {"orders": [o["view"] for o in talkshop_tools.own_orders(user.user_id, limit=5)]}


mcp.add_provider(cards)


# Streamable HTTP, stateless: each ChatGPT call is one request, nothing kept per session.
mcp_http_app = mcp.http_app(path="/mcp", stateless_http=True)
