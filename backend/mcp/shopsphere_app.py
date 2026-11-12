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
Card payment buttons are app-only. In the conversation the model pays only through place_order,
which needs the shopper's explicit yes, a server-signed authorization for the exact order
(review_order) and Claude's own permission prompt (destructive tool).
Every tool call and the services behind it are recorded in the shopper's trace
(backend/observability/protocol_trace.py, shown at /trace/<id>).

Deliberately separate from backend/mcp/server.py: that internal server has
user-scoped tools (create_checkout with a default user) that must never be public.
"""
import base64
import json
import os
import re
import secrets
import time
from functools import lru_cache
from io import BytesIO
from typing import Optional

from fastmcp import FastMCP
from fastmcp.apps.app import FastMCPApp
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.tools.base import ToolResult, _prefab_to_tool_result   # fastmcp==4.0.10 (pinned)
from mcp.types import TextContent
from prefab_ui.actions import SetState, ShowToast
from prefab_ui.actions.mcp import CallTool
from prefab_ui.app import PrefabApp
from prefab_ui.components import Button, Card, CardContent, CardFooter, Column, Grid, H3, Image, Muted, Row, Select, \
    SelectOption, Text
from prefab_ui.components.control_flow import If
from prefab_ui.rx import ERROR, RESULT, STATE
from sqlalchemy.orm import Session

from backend.db.init_db import CATALOG_IMAGE_DIR, get_engine
from backend.db.schema import User
from backend.routers import auth as account
from backend.mcp import card_sections as screens
from backend.mcp.auth import (current_customer, issue_purchase_authorization, optional_customer, public_base_url,
                              read_purchase_authorization, read_visitor_cart, visitor_cart_id)
from backend.observability import protocol_trace as pt
from backend.shop import card_approval, guest_wallet, notifications, payment_routing, profile, ratelimit
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

   Product questions ("is it waterproof?", "what are its reviews?"): call get_shopsphere_product
   and answer ONLY from what it returns (description, highlights, rating, review count). If a
   feature isn't listed, say ShopSphere's catalog doesn't list it. ShopSphere has an average rating
   and a review count, not review texts: never invent reviews. Keep the product the shopper chose
   in mind through these questions.

Buying is a conversation. The shopper chooses the product and authorizes the purchase; you do
the rest with the tools below. No forms, no website.
4. "I want the first one": call add_to_cart with its product_id, size and colour (ask only for
   what's missing). Confirm the item, the real price and the delivery estimate it returns.
   Before sign-in, results carry a visitor_cart id: pass that same visitor_cart to every later
   search_shopsphere, get_shopsphere_product, add_to_cart, show_cart, checkout and guest_checkout
   call in this chat.
   Keep the line_id add_to_cart returns: buy that line (line_ids=[line_id]).
5. "Let's buy it": if the shopper isn't signed in, ask ONE question: "Would you like to use your
   ShopSphere account or continue as a guest?" Don't pick for them.
   - Account (sign in or create one): call checkout with visitor_cart and line_ids. ShopSphere's
     sign-in page opens (it also has "Create an account"); the cart comes along.
   - Guest: call guest_checkout with visitor_cart and line_ids. NEVER call checkout,
     update_checkout_choices, finish_new_card_payment or my_orders for a guest: those are for
     signed-in accounts and would ask a guest to sign in. Guests never sign in.
6. Guest checkout, step by step (pass the guest_token from guest_checkout every time):
   a. If guest_checkout's saved_profile.available is true, ask: "I have your shipping information
      available (<name>, <ship_to>). Would you like me to use it for this order?"
      Yes → use_guest_profile(use_shipping=true). No → ask for name, email and shipping address
      in the chat and call update_guest_checkout, then use_guest_profile(use_shipping=false).
   b. Tell the estimated delivery date and the payment methods use_guest_profile returns (e.g.
      "Visa •••• 4242 (sandbox)") and ask which one to use.
   c. Call review_order with that payment_method_id. Show the order summary: product, size,
      quantity, price, shipping, tax, total, delivery date and the card, then ask: "Do you
      authorize me to place this order?"
   d. Only after a clear yes, call place_order with the authorization from review_order and
      customer_confirmed=true. A no, or anything unclear, means call decline_order: nothing is
      charged. Never treat silence, "maybe" or a question as a yes.
7. Signed-in customers: checkout shows their order (saved address and card; Chase offers,
   points). They can confirm and pay on the card, or in the chat: review_order(checkout_id,
   payment_method_id/delivery/use_points as asked), then the same authorize question and
   place_order(checkout_id, authorization, customer_confirmed=true) or decline_order.
8. place_order returns the real Order ID: tell it with the total, delivery date and where the
   confirmation email went, then add one line "Technical trace: <trace_url>". If place_order says
   verification_required (a new card), give the secure page link: the card is typed there, never
   in the chat; afterwards call finish_guest_payment (guest) or finish_new_card_payment (account).
   Never ask for card numbers, expiry or CVC; if the shopper types one, tell them not to.
   Never say an order is placed unless a tool returned its order_id.
9. The cards also work by themselves (Add to cart, cart, Checkout, Approve & pay, Track); they
   never send you chat messages, so only answer what the shopper types.
10. Order tracking: signed-in customers use my_orders. Guests use track_order with the Order ID
   and the email used at checkout. Both show a tracking card; add at most one short sentence.
Payments, card networks, issuers and the guest profile's sandbox cards are simulated.
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


def delivery_estimate(delivery_days) -> dict:
    """ShopSphere's own delivery dates for an order placed today (same rule as checkout)."""
    d = checkout_service.delivery_dates(int(delivery_days or 5))
    return {"standard": d["standard"].isoformat(), "express": d["express"].isoformat(),
            "express_price": checkout_service.EXPRESS_PRICE, "standard_price": 0.0}


@mcp.tool(annotations=READ_ONLY)
def get_shopsphere_product(product_id: str, size: Optional[str] = None, visitor_cart: Optional[str] = None) -> dict:
    """Details of one ShopSphere product (from search_shopsphere) for the shopper's questions:
    description, highlights, price, average rating and review count, every size and colour with
    stock, and the delivery estimate. Answer only from these facts: a feature that isn't listed
    (e.g. waterproof) isn't in ShopSphere's catalog, and there are no review texts to quote.
    Pass size to see which colours are in stock in that size, and the chat's visitor_cart if any."""
    p = talkshop_tools.get_product(product_id)
    if not p:
        return {"store": "ShopSphere", "found": False, "message": f"No ShopSphere product {product_id}."}
    avail = talkshop_tools.check_variant(product_id, size=size)
    out = {"store": "ShopSphere", "found": True, **_card(p, avail, size, None),
           "all_colors": [{"name": c["name"], "in_stock": c.get("available")} for c in avail.get("colors", [])],
           "sizes": avail.get("sizes", []),
           "reviews": {"average_rating": p.get("rating"), "review_count": p.get("review_count"),
                       "review_texts_available": False,
                       "note": "ShopSphere's catalog has the average rating and number of reviews only; no "
                               "individual review texts. Don't quote or invent reviews."},
           "delivery_estimate": delivery_estimate(p.get("delivery_days")),
           "answering_questions": "Use only the description and highlights for features. If a feature (waterproof, "
                                  "wide fit…) isn't mentioned, say ShopSphere's catalog doesn't list it."}
    pt.record(pt.MCP_SERVER, "Catalog service (SneakPeek)", pt.IN_PROCESS, "Product details (clarification)",
              request=f"{product_id}" + (f", size {size}" if size else ""),
              response=f"{p['name']}: ${p.get('price')}, rating {p.get('rating')} from {p.get('review_count')} reviews; "
                       f"highlights {', '.join(p.get('tags') or []) or '—'}",
              pattern="UCP-inspired product discovery")
    return out


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
    """Product cards for one search: photo, price, size and colour pickers, Add to cart. After an
    add, "View cart & checkout" opens the cart in this same card (a direct tool call), and from
    there checkout, guest checkout, payment and tracking all happen in place."""
    items, size = result["products"], result.get("size")
    cart_arg = {"visitor_cart": result["visitor_cart"]} if result.get("visitor_cart") else {}
    signed_in = optional_customer() is not None
    state: dict = screens.shop_state(cart=screens.cart_state(None, result.get("visitor_cart"), signed_in),
                                     has_products=True)
    S = STATE
    with Column(gap=3) as view:
        with If(~S.cart_open & ~S.guest_open & ~S.co_open & ~S.track_open):
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
                    state[f"added{i}"] = state[f"details{i}"] = False
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
                                    for sz in sizes:
                                        SelectOption(value=sz, label=f"{it.get('option_label') or 'Size'} {sz}")
                            if colours:
                                with Select(name=f"color{i}", value=state[f"color{i}"], size="sm"):
                                    for c in colours:
                                        SelectOption(value=c, label=c)
                            with If(getattr(S, f"details{i}")):
                                Muted(it.get("description") or "")
                                if it.get("highlights"):
                                    Muted(" · ".join(it["highlights"][:6]))
                                Muted(f"Delivery in about {it.get('delivery_days') or 5} days.")
                        with CardFooter(), Column(gap=2):
                            with Row(gap=2):
                                Button("Add to cart", size="sm", on_click=CallTool("add_to_cart", arguments={
                                    "product_id": it["product_id"],
                                    "size": getattr(S, f"size{i}") if sizes else None,
                                    "color": getattr(S, f"color{i}") if colours else None, "quantity": 1,
                                    **cart_arg},
                                    onSuccess=[SetState(f"added{i}", True),
                                               ShowToast("Added to your ShopSphere cart", variant="success")],
                                    onError=_failed_toast("Couldn't add it")))
                                Button("Details", variant="outline", size="sm",
                                       on_click=SetState(f"details{i}", ~getattr(S, f"details{i}")))
                            with If(getattr(S, f"added{i}")):
                                Button("View cart & checkout", variant="secondary", size="sm", on_click=_open_cart())
        screens.shop_sections()
    return view, state


def _open_cart() -> CallTool:
    """Open the cart screen in this card: a direct call (view_cart), no chat message."""
    return CallTool("view_cart", arguments={"visitor_cart": STATE.visitor_cart},
                    onSuccess=[*(SetState(k, getattr(RESULT, k)) for k in screens.CART_KEYS),
                               SetState("cart_open", True)],
                    onError=_failed_toast("Couldn't open the cart"))


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
    filters = ", ".join(f"{k} {v}" for k, v in (("category", category), ("size", result.get("size")), ("colour", color),
                                                ("under $", max_price), ("brand", brand)) if v)
    pt.record(pt.MCP_SERVER, "Catalog service (SneakPeek)", pt.IN_PROCESS, "Product search",
              request=f"\"{query}\"" + (f" · {filters}" if filters else ""),
              response=(f"{len(result['products'])} in stock: " + "; ".join(
                  f"{p['name']} ${p['price']}" for p in result["products"])) if result["found"] else "No match",
              status="success" if result["found"] else "info", pattern="UCP-inspired product discovery")
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
        pt.bind(pt.trace_id_for(vid))
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
    pt.bind(pt.trace_id_for(vid))
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
    qty = max(1, min(int(quantity or 1), 10))
    try:
        cart, line_id = talkshop_tools.add_to_cart(uid, sku, qty)
    except CartError as exc:
        raise ToolError(exc.message)
    pt.record(pt.MCP_SERVER, "Cart service (CartUp)", pt.IN_PROCESS, "Add to cart",
              request=f"{p['name']} · {_variant_label(size, color)} × {qty}",
              response=f"In stock; cart now {cart['item_count']} item(s), subtotal ${cart['subtotal']:.2f}",
              pattern="ACP-style cart")
    out = {"added": p["name"], "product_id": p["product_id"], "sku": sku, "size": size, "color": color,
           "quantity": qty, "unit_price": p.get("price"), "line_total": round((p.get("price") or 0) * qty, 2),
           "currency": p.get("currency", "USD"), "line_id": line_id,
           "delivery_estimate": delivery_estimate(p.get("delivery_days")),
           "cart_items": cart["item_count"], "cart_subtotal": cart["subtotal"],
           "next_step": "Confirm the item, its price and the delivery estimate. Nothing has been charged. When the "
                        "shopper wants to buy it, buy this line (line_ids=[line_id])."}
    if cart_id:
        out.update(visitor_cart=cart_id, next_step=out["next_step"] + " They aren't signed in: ask whether to use "
                   "their ShopSphere account (checkout with visitor_cart) or continue as a guest (guest_checkout "
                   "with visitor_cart). Pass visitor_cart to show_cart, checkout and guest_checkout.")
    return out


@cards.tool()
def update_cart_line(line_id: str, quantity: int, visitor_cart: Optional[str] = None) -> dict:
    """App-only (cart card buttons): set a cart line's quantity; 0 removes it."""
    uid, cart_id = _shopper(visitor_cart)
    with Session(get_engine()) as db:
        try:
            cart_service.set_quantity(db, uid, line_id, int(quantity))
        except CartError as exc:
            raise ToolError(exc.message)
    cart = talkshop_tools.get_cart(uid)
    return {"cart_items": cart["item_count"], "cart_subtotal": cart["subtotal"], **_cart_screen(cart, cart_id)}


def _cart_screen(cart: dict, cart_id: Optional[str]) -> dict:
    return screens.cart_state(cart, cart_id, signed_in=cart_id is None, photo=_photo)


@cards.tool()
def view_cart(visitor_cart: Optional[str] = None) -> dict:
    """App-only: "View cart & checkout" on a product card. The cart's lines and totals, for the card
    to show the cart in place (no chat message). Nothing is charged."""
    uid, cart_id = _shopper(visitor_cart)
    return _cart_screen(talkshop_tools.get_cart(uid), cart_id)


def _cart_view(cart: dict, cart_id: Optional[str] = None) -> tuple[Column, dict]:
    """The cart card: the cart screen, open. Checkout (signed in or not), guest checkout, payment and
    tracking then open in this same card."""
    state = screens.shop_state(cart=_cart_screen(cart, cart_id))
    state["cart_open"] = True
    with Column(gap=3) as view:
        screens.shop_sections()
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


_checkout_state = screens.checkout_state


def _checkout_view(co: dict) -> tuple[Column, dict]:
    """The checkout card (checkout tool): the shared checkout screen, open."""
    with Column(gap=3) as view:
        screens.shop_sections()
    return view, screens.shop_state(checkout=screens.checkout_card_state(co))


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


def _prepare_checkout(user, line_ids: Optional[list[str]] = None, delivery: Optional[str] = None,
                      use_points: bool = False, address_id: Optional[str] = None,
                      payment_method_id: Optional[str] = None, visitor_cart: Optional[str] = None) -> dict:
    """A checkout for the signed-in customer's cart (a visitor cart from before sign-in comes along)."""
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
    return _apply_choices(user.user_id, co, address_id, payment_method_id, delivery, True if use_points else None)


@cards.tool()
def open_checkout(visitor_cart: Optional[str] = None) -> dict:
    """App-only: the cart screen's Checkout / Sign in & check out button. Needs the shopper's
    ShopSphere sign-in (without it the connector answers 401 and Claude opens the sign-in page).
    Returns the checkout for the card to show in place. Nothing is charged."""
    return _checkout_state(_prepare_checkout(current_customer(), visitor_cart=visitor_cart or None))


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
    co = _prepare_checkout(current_customer(), line_ids, delivery, use_points, address_id, payment_method_id,
                           visitor_cart)
    view, state = _checkout_view(co)
    data = {k: co.get(k) for k in ("checkout_id", "checkout_version", "subtotal", "offer_discount", "points_discount",
                                   "shipping", "tax", "total", "currency", "earn_points", "ready", "issues")}
    data.update({"items": [{"name": ln["name"], "size": ln.get("size"), "color": ln.get("color"),
                            "quantity": ln["quantity"]} for ln in co["lines"]],
                 "delivery": (co.get("delivery") or {}).get("method"), "arrives": (co.get("delivery") or {}).get("date"),
                 "ship_to": (co.get("address") or {}).get("display"),
                 "pay_with": state["c_pay_with_text"],
                 "saved_addresses": [{"address_id": a["address_id"], "label": a.get("label"), "display": a["display"]}
                                     for a in co.get("saved_addresses") or []],
                 "saved_payment_methods": [{"payment_method_id": c["payment_method_id"], "display": c["display"],
                                            "issuer": c.get("issuer_display")} for c in co.get("saved_payment_methods") or []],
                 "next_step": "The shopper can change address, card, delivery or points on the card (or ask you: call "
                              "update_checkout_choices), then approves on the card (Approve & pay, then the simulated "
                              "passkey). Never say the order is paid unless they tell you it went through."})
    return _ui(view, state, data)


_GUEST_WAY = ("This is a guest checkout, so no sign-in is needed: use the guest_token from guest_checkout with "
              "review_order (delivery, payment), place_order, finish_guest_payment, and track_order to track it.")


def _customer_for(checkout_id: Optional[str]):
    """The signed-in customer for a customer tool. A guest's checkout id gets the guest way, never a
    sign-in (McpSignIn lets such a call through instead of answering 401)."""
    user = optional_customer()
    if user:
        return user
    if checkout_id:
        with Session(get_engine()) as db:
            from backend.db.schema import Checkout
            co = db.query(Checkout).filter_by(checkout_id=str(checkout_id)[:40]).first()
            if co and checkout_service.is_guest_owner(db, co.user_id):
                pt.record(pt.MCP_SERVER, pt.CLAUDE, pt.MCP, "Guest kept out of account sign-in",
                          request=f"account-only tool called for guest checkout {co.checkout_id}",
                          response="Answered with the guest tools instead of a sign-in challenge", status="info",
                          correlation=co.checkout_id)
                raise ToolError(_GUEST_WAY)
    return current_customer()


@cards.tool(model=True)
def update_checkout_choices(checkout_id: str, address_id: Optional[str] = None, pay_with: Optional[str] = None,
                            delivery: Optional[str] = None, use_points: Optional[str] = None) -> dict:
    """Change a signed-in customer's open ShopSphere checkout (not for guests: they use review_order
    with their guest_token): shipping address (address_id), payment (a saved card's
    payment_method_id, "wallet", or "provider" = a new card on the secure payment page), delivery ("standard"/"express") and points ("yes"/"no").
    Totals and Chase offers are recalculated (Chase offers need a Chase card). Nothing is charged."""
    user = _customer_for(checkout_id)
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
    try:
        res = await _pay(user, checkout_id, checkout_version, final_amount, currency, consent_mode="button")
    except (CheckoutError, VicError) as exc:
        raise ToolError(exc.message)
    if res.get("status") == "provider_handoff_needed":
        raise ToolError("Choose \"a new card\" on the checkout card to pay on the secure payment page.")
    if res.get("status") != "authorized":
        raise ToolError(res.get("message") or f"Payment {res.get('status')}. Nothing was charged.")
    order = res.get("order") or {}
    return {"status": "authorized", "order_id": order.get("order_id"), "total": order.get("total"),
            "arrives": order.get("delivery_date"), "email": res.get("confirmation_email")}


async def _pay(user, checkout_id: str, checkout_version: int, final_amount: float, currency: str,
               consent_mode: str, guest: bool = False) -> dict:
    """The shopper's approval of one exact order, then payment: the same approval step for the way
    they pay (simulated passkey for a card on file, the authenticated instruction for Visa agent
    payments, none for the Wallet), then payment.confirm's GreenLight → PayIt → order path.
    consent_mode "button" = a card button; "typed" = the shopper's explicit yes in the conversation.
    A new card isn't paid here: {"status": "provider_handoff_needed"} (it is verified on the
    secure provider page). Raises CheckoutError / VicError; the engine's steps go to the trace."""
    uid = user.user_id
    scope = {"action": "PAY", "checkout_version": int(checkout_version), "final_amount": float(final_amount),
             "currency": currency}
    with Session(get_engine()) as db:
        co = checkout_service.get_checkout(db, uid, checkout_id)
        route = payment_routing.route(db, co).get("route")
        approval_id = instruction_id = None
        if route == "saved_card":
            started = card_approval.start(db, uid, checkout_id)
            approval_id = started["approval"]["approval_id"]
            card_approval.verify(db, uid, checkout_id, approval_id, started["passkey"]["nonce"], True)
        elif route == "vic" and not guest:
            instr, pk = vic_instructions.create(db, uid, checkout_id, consent_mode)
            instruction_id = instr.instruction_id
            vic_instructions.authenticate(db, uid, instruction_id, pk["challenge_id"], pk["nonce"], True)
        elif route == "provider_handoff":
            return {"status": "provider_handoff_needed"}
        elif route != "wallet" or guest:
            raise CheckoutError("NO_PAYMENT_METHOD", "Choose how to pay first." if route == "none" else
                                "Set up Visa agent payments on ShopSphere first, then try again.", 409)
    res = await payment_service.confirm(user, checkout_id, consent=True, consent_mode=consent_mode,
                                        instruction_id=instruction_id, approval_id=approval_id, guest=guest, **scope)
    _trace_engine(res, checkout_id)
    return res


def _trace_engine(res: dict, correlation: Optional[str]) -> None:
    """The payment engine's own steps (as it returned them) into the shopper's trace."""
    pt.from_engine(res.get("trace") or [], correlation=(res.get("order") or {}).get("order_id") or correlation)


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
    _trace_engine(res, checkout_id)
    if res.get("status") != "provider_handoff":
        raise ToolError(res.get("message") or f"Payment {res.get('status')}. Nothing was charged.")
    return {"status": "provider_handoff", "hosted_url": res["hosted_url"],
            "payment_session_id": res["payment_session_id"], "expires_at": res.get("expires_at")}


@cards.tool(model=True)
async def finish_new_card_payment(checkout_id: str, payment_session_id: Optional[str] = None) -> dict:
    """After the shopper finished on the secure payment page: ShopSphere asks the payment provider
    itself for the result and, if the card was verified, places the order (once). Call it when the
    shopper says they finished paying there. Nothing is charged unless they completed that page.
    Signed-in customers only: a guest uses finish_guest_payment with their guest_token."""
    user = _customer_for(checkout_id)
    if not payment_session_id:
        with Session(get_engine()) as db:
            payment_session_id = checkout_service.get_checkout(db, user.user_id, checkout_id).provider_session_id
        if not payment_session_id:
            raise ToolError("There's no open secure payment for this order. Tap Approve & pay on the checkout card.")
    try:
        res = await payment_service.complete_provider(user, checkout_id, payment_session_id[:80])
    except CheckoutError as exc:
        raise ToolError(exc.message)
    _trace_engine(res, checkout_id)
    if res.get("status") == "pending":
        raise ToolError("The secure payment page isn't finished yet. Enter the card there, then tap "
                        "\"I've finished paying\" again.")
    if res.get("status") != "authorized":
        raise ToolError(res.get("message") or f"Payment {res.get('status')}. Nothing was charged.")
    order = res.get("order") or {}
    return {"status": "authorized", "order_id": order.get("order_id"), "total": order.get("total"),
            "arrives": order.get("delivery_date"), "email": res.get("confirmation_email")}


# ── Guest checkout: no account, no sign-in ─────────────────────────────────────

_mask_email = screens.mask_email
_guest_state = screens.guest_state


def _guest_view(co: dict, token: str) -> tuple[Column, dict]:
    """The guest checkout card (guest_checkout): the shared guest screen, open."""
    with Column(gap=3) as view:
        screens.shop_sections()
    return view, screens.shop_state(guest=screens.guest_card_state(co, token))


def _guest(token: Optional[str]):
    with Session(get_engine()) as db:
        try:
            return guest_service.load(db, token)
        except CheckoutError as exc:
            raise ToolError(exc.message)


def _start_guest(visitor_cart: Optional[str], line_ids: Optional[list[str]] = None,
                 delivery: Optional[str] = None) -> tuple[dict, str]:
    """A guest checkout for this chat's visitor cart: (checkout snapshot, guest token). Used by
    guest_checkout (asked in the chat) and open_guest_checkout (the cart card's guest button)."""
    if optional_customer():                        # a valid sign-in: their own account checkout applies
        raise ToolError("You're signed in to ShopSphere, so use checkout: your saved details apply.")
    vid = read_visitor_cart(visitor_cart)
    if not vid:
        raise ToolError("I couldn't find this chat's cart. Show the cart (or add the items) first.")
    with Session(get_engine()) as db:
        try:
            return guest_service.start(db, vid, line_ids, delivery)
        except CheckoutError as exc:
            raise ToolError(exc.message)


@cards.tool()
def open_guest_checkout(visitor_cart: str) -> dict:
    """App-only: the cart card's "Check out as guest" button. Starts the guest checkout for this
    chat's visitor cart (the same one guest_checkout starts) and returns what the card needs to show
    the guest form in place, with no sign-in and no chat turn. Nothing is charged."""
    co, token = _start_guest(visitor_cart)
    state = screens.guest_card_state(co, token)
    return {k: state[k] for k in screens.GUEST_OPENED}


def _order_summary(co: dict) -> dict:
    """The order as ShopSphere computed it (never the model's numbers): what the shopper approves."""
    pm = co.get("payment_method") or {}
    paying = ("New card on the secure payment page" if co.get("pay_with") == "provider" else
              "ShopSphere Wallet" if co.get("pay_with") == "wallet" else
              (f"{pm.get('display')}{' (sandbox)' if str(pm.get('payment_method_id', '')).startswith('sbx_') else ''}"
               if pm else None))
    delivery = co.get("delivery") or {}
    return {
        "checkout_id": co["checkout_id"], "checkout_version": co.get("checkout_version"),
        "items": [{"name": ln["name"], "size": ln.get("size"), "color": ln.get("color"), "quantity": ln["quantity"],
                   "unit_price": ln.get("unit_price"), "line_total": round((ln.get("unit_price") or 0) * ln["quantity"], 2),
                   "in_stock": ln.get("in_stock", True)} for ln in co["lines"]],
        "subtotal": co.get("subtotal"), "discounts": (co.get("invoice") or {}).get("discounts") or [],
        "shipping": co.get("shipping"), "tax": co.get("tax"), "total": co.get("total"), "currency": co.get("currency"),
        "delivery": delivery.get("method"), "estimated_delivery": delivery.get("date"),
        "delivery_options": delivery.get("options"),
        "ship_to": (co.get("address") or {}).get("display"),
        "pay_with": paying, "ready": co.get("ready"), "issues": [i.get("message") or i.get("code")
                                                               for i in co.get("issues") or []],
    }


def _trace_url(trace_id: Optional[str]) -> Optional[str]:
    if not trace_id:
        return None
    from fastmcp.server.dependencies import get_http_headers
    try:
        base = public_base_url(get_http_headers(include_all=True))
    except Exception:
        base = public_base_url()
    return f"{base}/trace/{trace_id}"


@cards.tool(model=True)
def guest_checkout(visitor_cart: str, line_ids: Optional[list[str]] = None,
                   delivery: Optional[str] = None) -> dict:
    """Start a GUEST checkout for this chat's ShopSphere cart: no account, no sign-in, no form. Use
    it when the shopper says to continue as a guest. ShopSphere prices the order (live price, stock,
    tax, delivery) and returns a guest_token for the next steps, plus saved_profile: the shipping
    information the shopper has authorized for checkout (if available). Next, ask whether to use
    it, then call use_guest_profile. Nothing is charged. Guests don't get points, the Wallet or
    Chase offers.

    Args:
        visitor_cart: the visitor_cart id from earlier results in this chat (required).
        line_ids: the cart line(s) the shopper is buying (add_to_cart's line_id); default: whole cart.
        delivery: optional "standard" or "express".
    """
    co, token = _start_guest(visitor_cart, line_ids, delivery)
    pt.record(pt.MCP_SERVER, "Checkout service (CartUp)", pt.IN_PROCESS, "Guest checkout created",
              request=f"{co['item_count']} item(s), no account, no sign-in",
              response=f"{co['checkout_id']}: subtotal ${co['subtotal']:.2f}, live price + stock checked",
              pattern="ACP-style create checkout", correlation=co["checkout_id"])
    pt.record(pt.MCP_SERVER, pt.CLAUDE, pt.MCP, "Guest checkout token issued",
              request="Capability for this one checkout", response="Signed JWT (HS256), 2 hours, names this visitor + "
              "checkout only", correlation=co["checkout_id"])
    offer = guest_wallet.offer()
    out = {**_order_summary(co), "guest": True, "guest_token": token, "ready": False, "saved_profile": offer}
    if offer.get("available"):
        out["pay_with"] = "not chosen yet"                 # the shopper picks after use_guest_profile
    out["next_step"] = (
        f"Ask: \"I have your shipping information available ({offer['name']}, {offer['ship_to']}). Would you like "
        "me to use it for this order?\" Yes → use_guest_profile(guest_token, use_shipping=true). No → ask for name, "
        "email and shipping address, call update_guest_checkout, then use_guest_profile(use_shipping=false)."
        if offer.get("available") else
        "Ask for the guest's name, email and shipping address, call update_guest_checkout with guest_token, then "
        "review_order(guest_token). They pay with a new card on the secure payment page.")
    return out


@cards.tool(model=True)
def use_guest_profile(guest_token: str, use_shipping: bool = True) -> dict:
    """The guest answered whether to use the shipping information from their authorized profile
    (guest_checkout's saved_profile). use_shipping=true ships this order there (this order only, no
    account). Returns the estimated delivery date, the updated total and the payment methods
    available from the profile (sandbox cards, shown as network + last 4 only), to ask which one
    to use. Nothing is charged."""
    return _use_profile(guest_token, use_shipping)


def _use_profile(guest_token: str, use_shipping: bool) -> dict:
    user, checkout_id = _guest(guest_token)
    prof = guest_wallet.load()
    if not prof:
        raise ToolError("No authorized profile is set up on this ShopSphere server. Ask the guest for their name, "
                        "email and shipping address and call update_guest_checkout.")
    with Session(get_engine()) as db:
        try:
            if use_shipping:
                guest_wallet.apply_shipping(db, user.user_id, checkout_id, prof)
            pms = guest_wallet.payment_methods(db, user.user_id, prof)
            co = checkout_service.snapshot(db, checkout_service.get_checkout(db, user.user_id, checkout_id))
        except CheckoutError as exc:
            raise ToolError(exc.message)
    addr = co.get("address") or {}
    if use_shipping:
        pt.record("Demo guest wallet (configured profile)", pt.MCP_SERVER, pt.IN_PROCESS,
                  "Shipping information shared (shopper said yes)",
                  request="Shopper agreed to use their authorized profile for this order",
                  response=f"Name + {addr.get('city')}, {addr.get('state')} address applied to this order only; "
                           "email kept for the confirmation", correlation=checkout_id,
                  pattern="Fictional demo profile")
    pt.record("Demo guest wallet (configured profile)", "Mock gateway / PSP vault", pt.SIMULATED,
              "Sandbox payment tokens issued",
              request=f"{len(pms)} sandbox card(s) for this guest", layer="simulated", correlation=checkout_id,
              response="; ".join(f"{p.brand} •••• {p.last4} → opaque token" for p in pms) +
                       ". The assistant sees network + last 4 only", pattern="Simulated PSP tokens")
    out = {**_order_summary(co), "shipping_from_profile": bool(use_shipping), "pay_with": "not chosen yet",
           "ready": False, "payment_methods": [guest_wallet.view(p) for p in pms],
           "next_step": "Tell the estimated delivery date and the payment methods, and ask which one to use. Then "
                        "call review_order(guest_token, payment_method_id)." if co.get("address") else
                        "Ask for the guest's name, email and shipping address and call update_guest_checkout, then "
                        "ask which payment method to use."}
    return out


def _guest_screen(guest_token: str) -> dict:
    """The guest card's refreshed values for this guest's checkout."""
    user, cid = _guest(guest_token)
    with Session(get_engine()) as db:
        co = checkout_service.snapshot(db, checkout_service.get_checkout(db, user.user_id, cid))
    st = screens.guest_state(co)
    return {k: st[k] for k in screens.GUEST_REFRESHED}


@cards.tool()
def card_use_guest_profile(guest_token: str) -> dict:
    """App-only: "Yes, use it" on the guest card. Ships this order to the shopper's authorized
    profile (this order only) and returns the profile's sandbox cards for the card to show."""
    out = _use_profile(guest_token, True)
    cards_ = out["payment_methods"]
    prof = guest_wallet.load() or {}
    return {**_guest_screen(guest_token), "profile_used": True,
            "g_cards": [{"id": c["payment_method_id"], "label": c["display"]} for c in cards_],
            # the profile's own (fictional) email, so the card's Track button can find the order later
            "track_email": (prof.get("contact") or {}).get("email", "")}


@cards.tool()
def card_review_guest_order(guest_token: str, payment_method_id: str, delivery: Optional[str] = None) -> dict:
    """App-only: "Review order" on the guest card. The same final check as review_order: live stock,
    price, tax, shipping, then the authorization request for exactly this order."""
    out = _review(guest_token, None, payment_method_id, delivery, None)
    if not out.get("authorization"):
        raise ToolError(out.get("next_step") or "This order isn't ready yet.")
    return {**_guest_screen(guest_token), "review_ready": True, "authorization": out["authorization"],
            "pay_with_text": f"Pay with: {out['pay_with']}"}


def _owner(guest_token: Optional[str], checkout_id: Optional[str]):
    """(user, checkout id, guest?) for the conversational checkout tools: a guest by their guest
    token, else the signed-in customer's own checkout. Never a sign-in for a guest."""
    if guest_token:
        user, cid = _guest(guest_token)
        return user, cid, True
    if not checkout_id:
        raise ToolError("Pass guest_token (guest checkout) or checkout_id (a signed-in customer's checkout).")
    user = _customer_for(checkout_id)
    with Session(get_engine()) as db:
        try:
            checkout_service.get_checkout(db, user.user_id, checkout_id)
        except CheckoutError as exc:
            raise ToolError(exc.message)
    return user, checkout_id, False


def _placed_order(db: Session, co) -> Optional[dict]:
    from backend.db.schema import Order
    if co.status != "paid":
        return None
    row = (db.query(Order).filter(Order.checkout_id == co.checkout_id, Order.display_id.isnot(None))
           .order_by(Order.id.desc()).first())
    return order_service.order_dict(db, row) if row else None


@cards.tool(model=True)
def review_order(guest_token: Optional[str] = None, checkout_id: Optional[str] = None,
                 payment_method_id: Optional[str] = None, delivery: Optional[str] = None,
                 use_points: Optional[str] = None) -> dict:
    """Final order review before the shopper authorizes payment (guests: guest_token; signed-in
    customers: checkout_id). Applies the chosen payment method (a payment_method_id from
    use_guest_profile or checkout, "wallet", or "provider" = a new card on the secure page),
    delivery ("standard"/"express") and points ("yes"/"no", customers only), then re-checks live
    stock, price, tax, shipping and the delivery date. Returns the summary to show and an
    authorization request for exactly this order (15 min). Ask "Do you authorize me to place this
    order?"; only on a clear yes call place_order. Nothing is charged."""
    return _review(guest_token, checkout_id, payment_method_id, delivery, use_points)


def _review(guest_token: Optional[str], checkout_id: Optional[str], payment_method_id: Optional[str],
            delivery: Optional[str], use_points: Optional[str]) -> dict:
    user, cid, guest = _owner(guest_token, checkout_id)
    uid = user.user_id
    with Session(get_engine()) as db:
        co = checkout_service.get_checkout(db, uid, cid)
        done = _placed_order(db, co)
        if done:
            return {"already_placed": True, "order_id": done["order_id"], "total": done.get("total"),
                    "next_step": "This order is already placed; nothing more to pay."}
        if co.status != "open":
            raise ToolError(f"This checkout is {co.status}. Start a new checkout from the cart.")
        try:
            if guest:
                pm = (payment_method_id or "").strip()
                if pm == "provider":
                    checkout_service.update_checkout(db, uid, cid, pay_with="provider")
                elif pm:
                    prof = guest_wallet.load()
                    options = guest_wallet.payment_methods(db, uid, prof) if prof else guest_wallet.existing(db, uid)
                    pick = next((c for c in options if pm == c.payment_method_id or pm.endswith(c.last4)), None)
                    if not pick:
                        raise ToolError("That payment method isn't one of this guest's options. Use a "
                                        "payment_method_id from use_guest_profile, or \"provider\" for a new card.")
                    checkout_service.update_checkout(db, uid, cid, payment_method_id=pick.payment_method_id)
                if delivery in ("standard", "express"):
                    checkout_service.update_checkout(db, uid, cid, delivery_method=delivery)
        except CheckoutError as exc:
            raise ToolError(exc.message)
    if not guest:
        snap = talkshop_tools.get_checkout(uid, cid)
        _apply_choices(uid, snap, None, payment_method_id, delivery, use_points)
    with Session(get_engine()) as db:
        co = checkout_service.get_checkout(db, uid, cid)
        issues = checkout_service.refresh(db, co)              # live stock + prices, right now
        db.commit()
        snap = checkout_service.snapshot(db, co, issues)
    summary = _order_summary(snap)
    pt.record(pt.MCP_SERVER, "Checkout service (CartUp)", pt.IN_PROCESS, "Order re-verified before authorization",
              request=f"{cid}: payment {summary['pay_with'] or 'not chosen'}, {summary['delivery']} delivery",
              response=f"Live stock + price + tax + shipping recomputed: total ${snap['total']:.2f}, version "
                       f"{snap['checkout_version']}, delivery {summary['estimated_delivery']}"
                       + ("" if snap["ready"] else f"; not ready: {', '.join(summary['issues'])}"),
              status="success" if snap["ready"] else "pending", pattern="ACP-style update checkout", correlation=cid)
    if not snap["ready"]:
        return {**summary, "next_step": "Not ready to authorize yet: " + "; ".join(summary["issues"])}
    pay_ref = snap.get("pay_with") if snap.get("pay_with") != "card" else (snap.get("payment_method") or {}).get(
        "payment_method_id", "card")
    auth = issue_purchase_authorization(uid, cid, snap["checkout_version"], snap["total"], snap["currency"],
                                        snap["checkout_hash"], pay_ref)
    pt.record(pt.MCP_SERVER, pt.CLAUDE, pt.MCP, "Purchase authorization requested",
              request="Exact order terms to show the shopper",
              response=f"Signed request (15 min) bound to {cid} v{snap['checkout_version']}, "
                       f"{snap['currency']} {snap['total']:.2f}, {summary['pay_with']}, order fingerprint",
              status="pending", pattern="AP2-style intent → cart mandate (concept only)", correlation=cid)
    return {**summary, "authorization": auth, "authorization_expires_in_minutes": 15,
            "next_step": "Show this summary (items with size, quantity and price; shipping; tax; total; estimated "
                         "delivery; payment method) and ask: \"Do you authorize me to place this order?\" Only "
                         "after a clear yes, call place_order with this authorization and customer_confirmed=true. "
                         "If they decline, call decline_order."}


@cards.tool(model=True)
async def place_order(authorization: str, customer_confirmed: bool, guest_token: Optional[str] = None,
                      checkout_id: Optional[str] = None) -> dict:
    """Place and pay for the order the shopper just authorized. Call ONLY after the shopper clearly
    said yes to review_order's summary, with that authorization and customer_confirmed=true.
    ShopSphere checks the authorization against the live order (same version, total, currency,
    payment method and fingerprint; a change means review_order again), runs the approval and the
    simulated payment, creates the order once and emails the confirmation. Calling it again returns
    the same order: it never charges twice. Guests: guest_token; customers: checkout_id."""
    user, cid, guest = _owner(guest_token, checkout_id)
    if customer_confirmed is not True:
        raise ToolError("Only place the order after the shopper explicitly authorizes it. Ask them first.")
    with Session(get_engine()) as db:
        co = checkout_service.get_checkout(db, user.user_id, cid)
        done = _placed_order(db, co)
        current = checkout_service.snapshot(db, co) if not done else None
    if done:
        pt.record(pt.MCP_SERVER, pt.CLAUDE, pt.MCP, "Repeated place_order: same order returned",
                  request="Retry after the order was placed", response=f"{done['order_id']}: not charged again",
                  status="info", pattern="Idempotency", correlation=done["order_id"])
        return {"status": "authorized", "duplicate": True, "order_id": done["order_id"], "total": done.get("total"),
                "arrives": done.get("delivery_date"), "trace_url": _trace_url(pt.current()),
                "next_step": "This order was already placed; it was not charged again."}
    claims = read_purchase_authorization(authorization)
    if not claims or claims.get("sub") != user.user_id or claims.get("chk") != cid:
        raise ToolError("That authorization isn't valid for this order (or it expired). Call review_order again and "
                        "ask the shopper to authorize the new summary.")
    pay_ref = current.get("pay_with") if current.get("pay_with") != "card" else (
        current.get("payment_method") or {}).get("payment_method_id", "card")
    same = (claims.get("ver") == current["checkout_version"] and claims.get("amt") == round(current["total"] * 100)
            and claims.get("cur") == current["currency"] and claims.get("fp") == (current["checkout_hash"] or "")[:16]
            and claims.get("pay") == pay_ref)
    pt.record(pt.CLAUDE, pt.MCP_SERVER, pt.MCP, "Customer purchase authorization",
              request=f"Shopper said yes in the chat (customer_confirmed=true) and allowed Claude's payment prompt; "
                      f"authorization for {cid} v{claims.get('ver')} {claims.get('cur')} {claims.get('amt', 0) / 100:.2f}",
              response="Matches the live order" if same else "Order changed since it was authorized: refused",
              status="success" if same else "failure", pattern="AP2-style scoped consent (concept only)",
              correlation=cid)
    if not same:
        raise ToolError("The order changed after the shopper authorized it (price, delivery, address or card), so "
                        "nothing was charged. Call review_order and ask them to authorize the new summary.")
    try:
        res = await _pay(user, cid, claims["ver"], claims["amt"] / 100, claims["cur"], consent_mode="typed",
                         guest=guest)
    except (CheckoutError, VicError) as exc:
        raise ToolError(exc.message)
    if res.get("status") == "provider_handoff_needed":
        page = await _open_secure_page(user, cid, claims["ver"], claims["amt"] / 100, claims["cur"], guest=guest)
        return {"status": "verification_required", "secure_page": page["hosted_url"],
                "trace_url": _trace_url(pt.current()),
                "next_step": "A new card is entered and verified on the secure payment page, never in the chat: give "
                             "the shopper this link. When they say they're done, call "
                             + ("finish_guest_payment with guest_token." if guest else
                                "finish_new_card_payment with checkout_id.")}
    if res.get("status") != "authorized":
        raise ToolError(res.get("message") or f"Payment {res.get('status')}. Nothing was charged.")
    order = res.get("order") or {}
    from backend.db.schema import Order
    with Session(get_engine()) as db:
        row = db.query(Order).filter_by(order_id=order.get("internal_order_id")).first()
        mail = (notifications.confirmation_for(db, row) if row else None) or {}
    return {"status": "authorized", "order_id": order.get("order_id"), "total": order.get("total"),
            "currency": order.get("currency", "USD"), "arrives": order.get("delivery_date"),
            "paid_with": res.get("card") or (order.get("payment") or {}).get("display"),
            "confirmation_sent_to": _mask_email(mail.get("to")), "email_delivery": mail.get("delivery") or "none",
            "trace_url": _trace_url(pt.current()),
            "next_step": "Confirm the Order ID, total and estimated delivery, and where the confirmation email went. "
                         "Then one line: \"Technical trace: <trace_url>\". Guests track it with track_order (Order ID "
                         "+ their email); customers with my_orders."}


@cards.tool(model=True)
def decline_order(guest_token: Optional[str] = None, checkout_id: Optional[str] = None, reason: str = "") -> dict:
    """The shopper did NOT authorize the order (said no, or wants to stop). Records that nothing was
    approved; no payment and no order are created. The checkout stays open in case they change
    their mind (they can review_order again)."""
    user, cid, guest = _owner(guest_token, checkout_id)
    payment_service.record_approval_cancelled(user.user_id, cid, "CUSTOMER_DECLINED")
    pt.record(pt.CLAUDE, pt.MCP_SERVER, pt.MCP, "Customer declined authorization",
              request=(reason or "Shopper said no")[:120], response="Nothing approved, charged or ordered",
              status="info", pattern="AP2-style consent (concept only)", correlation=cid)
    return {"status": "not_placed", "charged": False, "order_created": False,
            "next_step": "Tell the shopper nothing was ordered or charged. Their cart is kept."}


@cards.tool(model=True)
def update_guest_checkout(guest_token: str, delivery: Optional[str] = None, full_name: str = "", email: str = "",
                          line1: str = "", line2: str = "", city: str = "", state: str = "",
                          postal_code: str = "") -> dict:
    """Guest checkout: save the guest's own contact + shipping details (when they don't use their
    authorized profile, or on the guest card) and/or the delivery choice ("standard"/"express").
    ShopSphere validates them and recalculates the total. Never pass card details here."""
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
    _trace_engine(res, checkout_id)
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


# ── Order tracking: a tracking card (timeline, route map, photos), not just text ──

_TRACKING_NOTE = ("The tracking card shows the status, arrival date, route map, shipment timeline, items and "
                  "totals. Add at most one short sentence; don't repeat the card's details as a list.")


def _caller() -> str:
    from fastmcp.server.dependencies import get_http_headers
    return (get_http_headers(include_all=True).get("x-forwarded-for") or "").split(",")[0].strip() or "mcp"


def _find_tracked(db: Session, order_id: str, email: Optional[str]):
    """A signed-in customer's own order, or any order by Order ID + checkout email (rate limited;
    every failure looks the same). None if nothing matches."""
    order_id = (order_id or "")[:20]
    user = optional_customer()
    order = order_service.find_order(db, user.user_id, order_id) if user else None
    if order is None and email:
        if ratelimit.allow_tracking(_caller(), order_id):
            order = order_service.find_for_tracking(db, order_id, (email or "")[:255])
            if order is None:
                ratelimit.tracking_failed(order_id)
    return order


def _tracking_screen(view: dict, email: Optional[str]) -> dict:
    return screens.tracking_state(view, email or "", photo=_photo)


def _tracking_card(tracking: Optional[dict] = None, orders: Optional[list] = None, message: str = ""):
    state = screens.shop_state(tracking=tracking, orders=orders)
    state["not_found_text"] = message
    with Column(gap=3) as view:
        with If(STATE.not_found_text):
            with Card():
                with CardContent(), Column(gap=1):
                    Text("Order not found", css_class="font-semibold")
                    Muted(message or "")
        screens.shop_sections()
    return view, state



@cards.ui(annotations=READ_ONLY)
def track_order(order_id: str, email: str) -> ToolResult:
    """Track any ShopSphere order (guest or customer) with its Order ID (e.g. SS-12345) and the
    email address used at checkout. No sign-in needed. Ask the shopper for both if missing. Shows a
    tracking card: status, arrival date, route map, shipment timeline, items with photos, carrier
    and tracking ID; never the full address or card number."""
    order_id, email = (order_id or "")[:20], (email or "")[:255]
    if ratelimit.allow_tracking(_caller(), order_id):
        with Session(get_engine()) as db:
            order = order_service.find_for_tracking(db, order_id, email)
            if order is not None:
                tracked = order_service.tracking_view(db, order)
                view, state = _tracking_card(tracking=_tracking_screen(tracked, email))
                return _ui(view, state, {"found": True, "order": tracked, "next_step": _TRACKING_NOTE})
        ratelimit.tracking_failed(order_id)
    missing = {"found": False, "message": order_service.NOT_FOUND}   # the same answer for every failure
    view, state = _tracking_card(message=order_service.NOT_FOUND)
    return _ui(view, state, missing)


@cards.tool()
def order_status(order_id: str, email: Optional[str] = None) -> dict:
    """App-only: "Track my order" / "Refresh status" on a card. A signed-in customer's own order, or
    any order with its Order ID + checkout email (rate limited, one generic answer). Returns the
    tracking screen's values."""
    with Session(get_engine()) as db:
        order = _find_tracked(db, order_id, email)
        if order is None:
            raise ToolError(order_service.NOT_FOUND)
        tracked = order_service.tracking_view(db, order)
    sh = tracked.get("shipment") or {}
    summary = " · ".join(x for x in (f"{tracked['order_id']}: {sh.get('status_label') or tracked['status']}",
                                     f"tracking {sh['tracking_number']}" if sh.get("tracking_number") else "",
                                     f"estimated delivery {sh['estimated_delivery']}"
                                     if sh.get("estimated_delivery") else "") if x)
    return {"found": True, "order_id": tracked["order_id"], "summary": summary,
            **_tracking_screen(tracked, email)}


@cards.tool()
def advance_demo_shipment(order_id: str, email: Optional[str] = None) -> dict:
    """App-only demo control ("Simulate next step" on the tracking card): moves this order's
    simulated shipment one step, like the Track Order page's demo control on the website. The same
    lookup as tracking (own order, or Order ID + checkout email). No real carrier is involved."""
    from backend.shop import shipping
    with Session(get_engine()) as db:
        order = _find_tracked(db, order_id, email)
        if order is None:
            raise ToolError(order_service.NOT_FOUND)
        if not shipping.advance(db, order):
            raise ToolError("This order has already been delivered.")
        tracked = order_service.tracking_view(db, order)
    return {"found": True, "order_id": tracked["order_id"], **_tracking_screen(tracked, email)}


@cards.ui(annotations=READ_ONLY)
def my_orders(order_id: Optional[str] = None) -> ToolResult:
    """The signed-in shopper's recent ShopSphere orders (or one order) as a card: photo, status and
    progress for each, and Track opens the full tracking (timeline, route map). Guests (no account)
    use track_order with their Order ID and email instead."""
    user = current_customer()
    if order_id:
        one = talkshop_tools.own_order(user.user_id, order_id)
        data = {"orders": [one] if one else [], "found": bool(one)}
        if one:
            data["next_step"] = _TRACKING_NOTE
            view, state = _tracking_card(tracking=_tracking_screen(one, None))
        else:
            view, state = _tracking_card(message=f"No order {order_id[:20]} on your ShopSphere account.")
        return _ui(view, state, data)
    orders = [o["view"] for o in talkshop_tools.own_orders(user.user_id, limit=5)]
    view, state = _tracking_card(orders=[screens.order_row(o, photo=_photo) for o in orders])
    data = {"orders": orders}
    if orders:
        data["next_step"] = ("The card lists these orders with status and a Track button for the full tracking. "
                             "Add at most one short sentence.")
    return _ui(view, state, data)


# Claude asks the shopper before running a tool unless it is marked read-only. ShopSphere asks only
# where the shopper commits to something: starting a checkout and approving a payment. Browsing,
# the cart, filling in checkout choices, finishing on the secure page (after its own approval)
# and tracking run on a click. The server's own controls (sign-in, guest token, the exact approved
# version + total, the provider's verified result) apply either way.
_ASK_TO_CHECKOUT = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
_ASK_TO_PAY = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
_NO_PROMPT = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
# The conversational guest checkout (guest_checkout → use_guest_profile → review_order) only prepares
# the order; the shopper commits at place_order, which Claude always asks about (destructive).
APPROVAL = {"checkout": _ASK_TO_CHECKOUT, "open_checkout": _ASK_TO_CHECKOUT,
            "open_guest_checkout": _ASK_TO_CHECKOUT, "approve_and_pay": _ASK_TO_PAY,
            "start_new_card_payment": _ASK_TO_PAY, "start_guest_payment": _ASK_TO_PAY, "place_order": _ASK_TO_PAY}


# ── The agent communication trace: every tool call, as it ran ───────────────────

_SECRET_ARGS = {"guest_token": "‹guest token›", "authorization": "‹purchase authorization›",
                "visitor_cart": "‹visitor cart›", "payment_session_id": "‹payment session›"}
_PRIVATE_ARGS = {"line1", "line2", "postal_code", "full_name"}


def _args_summary(args: dict) -> str:
    parts = []
    for k, v in args.items():
        if v in (None, "", [], {}):
            continue
        if k in _SECRET_ARGS:
            parts.append(f"{k}={_SECRET_ARGS[k]}")
        elif k in _PRIVATE_ARGS:
            parts.append(f"{k}=‹provided›")
        elif k == "email":
            parts.append(f"email={_mask_email(str(v))}")
        else:
            parts.append(f"{k}={json.dumps(v) if not isinstance(v, str) else v}")
    return ", ".join(parts)[:400] or "(no arguments)"


def _result_summary(data) -> str:
    if not isinstance(data, dict):
        return "Card shown"
    if data.get("products") is not None:
        return (f"{len(data['products'])} product(s): " + "; ".join(
            f"{p.get('name')} ${p.get('price')}" for p in data["products"])) if data.get("found") else "No match"
    keys = ("found", "status", "order_id", "added", "unit_price", "line_total", "total", "estimated_delivery",
            "arrives", "ready", "pay_with", "cart_items", "item_count", "duplicate", "charged", "message")
    bits = [f"{k}={data[k]}" for k in keys if data.get(k) not in (None, "", [])]
    if data.get("payment_methods"):
        bits.append("payment options: " + ", ".join(p.get("display", "") for p in data["payment_methods"]))
    if (data.get("saved_profile") or {}).get("available"):
        bits.append("authorized profile available")
    if data.get("authorization"):
        bits.append("authorization request issued")
    if data.get("guest_token"):
        bits.append("guest token issued")
    if data.get("secure_page"):
        bits.append("secure payment page link")
    return ", ".join(bits)[:400] or "OK"


def _trace_for_call(args: dict) -> Optional[str]:
    """The shopper this call belongs to: guest token → visitor; visitor cart → visitor; else the
    signed-in customer (whose trace takes in their visitor rows once the cart comes along)."""
    from backend.mcp.auth import read_guest_token
    found = read_guest_token(args.get("guest_token")) if args.get("guest_token") else None
    if found:
        return pt.trace_id_for(found[0])
    vid = read_visitor_cart(args.get("visitor_cart"))
    try:
        user = optional_customer()
    except Exception:
        user = None
    if user:
        mine = pt.trace_id_for(user.user_id)
        if vid:
            pt.merge(pt.trace_id_for(vid), mine)             # the visitor signed in: one story
        return mine
    return pt.trace_id_for(vid) if vid else None


class _TraceToolCalls(Middleware):
    """Records each tools/call Claude makes (request + response summary, status, timing); the
    services the tool used add their own rows inside it (see protocol_trace)."""

    async def on_call_tool(self, context, call_next):
        name = getattr(context.message, "name", "?")
        args = dict(getattr(context.message, "arguments", None) or {})
        start = time.time()
        try:
            token = pt.open_scope(_trace_for_call(args))
        except Exception:
            token = pt.open_scope(None)
        status, response = "success", ""
        try:
            result = await call_next(context)
            data = getattr(result, "structured_content", None)
            try:                                       # what the model received (a card's data is its text)
                data = json.loads(result.content[0].text)
            except Exception:
                pass
            response = _result_summary(data)
            if isinstance(data, dict) and data.get("status") in ("verification_required", "provider_handoff"):
                status = "pending"
            return result
        except Exception as exc:
            status, response = "failure", str(exc)[:300]
            raise
        finally:
            try:
                first = pt.event(pt.CLAUDE, pt.MCP_SERVER, pt.MCP, f"tools/call {name}", request=_args_summary(args),
                                 response=f"{response} ({(time.time() - start) * 1000:.0f} ms)", status=status,
                                 at=start)
                pt.close_scope(token, first)
            except Exception as exc:
                print(f"[trace] call not recorded: {type(exc).__name__}", flush=True)


def _set_approvals() -> None:
    from mcp.types import ToolAnnotations
    from fastmcp.tools.base import Tool
    for component in list(cards._local._components.values()):        # fastmcp==4.0.10 (pinned)
        if isinstance(component, Tool):
            component.annotations = ToolAnnotations(**APPROVAL.get(component.name, _NO_PROMPT))


_set_approvals()
mcp.add_provider(cards)
mcp.add_middleware(_TraceToolCalls())


# Streamable HTTP, stateless: each ChatGPT call is one request, nothing kept per session.
mcp_http_app = mcp.http_app(path="/mcp", stateless_http=True)
