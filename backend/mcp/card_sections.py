"""
The ShopSphere card's screens, drawn from card state (Prefab MCP App inside Claude).

Every card (product cards, cart, checkout, guest checkout) carries the same screens and moves
between them in place: a button calls a ShopSphere tool directly (`CallTool`), copies the
result into the card's state (`SetState`) and the next screen appears. No button sends a chat
message: Claude treats a message a card sends as not coming from the shopper and may ignore it,
and an extra model turn could also pick the wrong tool (e.g. the sign-in checkout for a guest).

  screen flags   cart_open, guest_open, co_open (product cards show their grid when all are off)
  cart           cart_lines, cart_count, cart_has_items, cart_subtotal_text, visitor_cart, signed_in
  guest          the guest checkout's values (unprefixed, as update_guest_checkout returns them)
  checkout       the signed-in checkout's values, prefixed c_ (as update_checkout_choices returns
                 them without the prefix)
  tracking       track_open (+ track_can_back), the tracked order's values prefixed t_ (as order_status
                 returns them); orders_list + o_orders for a signed-in shopper's recent orders
"""
from datetime import date, datetime, timezone
from typing import Callable, Optional

from prefab_ui.actions import OpenLink, SetState, ShowToast
from prefab_ui.actions.mcp import CallTool
from prefab_ui.components import (Badge, Button, Card, CardContent, CardFooter, Column, ForEach, Grid, H3, Icon,
                                  Image, Input, Muted, Progress, Row, Select, SelectOption, Separator, Text)
from prefab_ui.components.control_flow import If
from prefab_ui.rx import ERROR, RESULT, STATE

from backend.mcp.route_map import route_map

EXPRESS_PRICE = 9.99


def money(x) -> str:
    return f"${float(x or 0):.2f}"


def variant_label(size: Optional[str], color: Optional[str]) -> str:
    return " · ".join(x for x in (f"Size {size}" if size else None, color) if x)


def mask_email(email: Optional[str]) -> str:
    name, _, domain = (email or "").partition("@")
    return f"{name[:1]}***@{domain}" if domain else ""


def failed_toast(title: str = "Couldn't do that"):
    return ShowToast(title, description=ERROR, variant="error")


def _set_from_result(keys, prefix: str = "") -> list:
    return [SetState(f"{prefix}{k}", getattr(RESULT, k)) for k in keys]


# ── Cart ───────────────────────────────────────────────────────────────────────

CART_KEYS = ("cart_lines", "cart_count", "cart_has_items", "cart_subtotal_text", "visitor_cart", "signed_in")


def cart_state(cart: Optional[dict], cart_id: Optional[str], signed_in: bool,
               photo: Callable[[Optional[str], int], Optional[str]] = lambda u, px: None) -> dict:
    lines = (cart or {}).get("lines") or []
    return {
        "cart_lines": [{"line_id": ln["line_id"], "label": ln["name"], "qty": ln["quantity"],
                        "sub": " · ".join(x for x in (variant_label(ln.get("size"), ln.get("color")),
                                                      money(ln["unit_price"]) + " each") if x),
                        "stock_note": "" if ln.get("in_stock", True) else "Out of stock: remove it to check out.",
                        "photo": photo(ln.get("image_url"), 96) or ""} for ln in lines],
        "cart_count": int((cart or {}).get("item_count") or 0), "cart_has_items": bool(lines),
        "cart_subtotal_text": money((cart or {}).get("subtotal")), "visitor_cart": cart_id or "",
        "signed_in": bool(signed_in),
    }


def cart_section() -> None:
    S = STATE
    refresh = _set_from_result(CART_KEYS)
    H3("Your ShopSphere cart")
    with If(~S.cart_has_items):
        Muted("Your cart is empty. Ask me to find something!")
    with ForEach("cart_lines") as ln:
        with Card():
            with CardContent(), Row(gap=3, align="center"):
                with If(ln.photo):
                    Image(src=f"{ln.photo}", alt=f"{ln.label}", css_class="w-16 h-16 object-contain bg-white")
                with Column(gap=1, css_class="flex-1"):
                    Text(f"{ln.label}", css_class="font-medium")
                    Muted(f"{ln.sub}")
                    with If(ln.stock_note):
                        Muted(f"{ln.stock_note}")
                with Row(gap=2, align="center"):
                    for label, qty in (("−", ln.qty - 1), ("+", ln.qty + 1)):
                        Button(label, variant="outline", size="sm", on_click=CallTool(
                            "update_cart_line", arguments={"line_id": ln.line_id, "quantity": qty,
                                                           "visitor_cart": S.visitor_cart},
                            onSuccess=refresh, onError=failed_toast()))
                    Text(f"{ln.qty}", css_class="font-medium")
                    Button("Remove", variant="ghost", size="sm", on_click=CallTool(
                        "update_cart_line", arguments={"line_id": ln.line_id, "quantity": 0,
                                                       "visitor_cart": S.visitor_cart},
                        onSuccess=refresh, onError=failed_toast()))
    with If(S.cart_has_items):
        Muted(f"Subtotal: {S.cart_subtotal_text}. Offers, points, delivery and tax are added at checkout.")
        open_checkout = CallTool(
            "open_checkout", arguments={"visitor_cart": S.visitor_cart},
            onSuccess=[*_set_from_result(CHECKOUT_KEYS, "c_"), SetState("co_open", True)],
            onError=ShowToast("Sign in to check out", variant="error",
                              description="Sign in (or create your account) in the window Claude opens, then tap "
                                          "the button again. Your cart comes with you."))
        with If(S.signed_in):
            Button("Checkout", on_click=open_checkout)
        with If(~S.signed_in):
            Text("How would you like to check out?", css_class="font-medium")
            Muted("Sign in to your ShopSphere account, create one (your cart comes with you), or check out as a "
                  "guest with no account.")
            with Row(gap=2, css_class="flex-wrap"):
                Button("Sign in & check out", on_click=open_checkout)
                Button("Create account & check out", variant="outline", on_click=open_checkout)
                Button("Check out as guest", variant="secondary", on_click=CallTool(
                    "open_guest_checkout", arguments={"visitor_cart": S.visitor_cart},
                    onSuccess=[*_set_from_result(GUEST_OPENED), SetState("guest_open", True)],
                    onError=failed_toast("Couldn't start guest checkout")))
            Muted("Create account: choose \"Create an account\" on the ShopSphere sign-in page that opens.")
    with If(S.has_products):
        Button("Back to products", variant="ghost", size="sm", on_click=SetState("cart_open", False))


# ── Guest checkout ─────────────────────────────────────────────────────────────

GUEST_REFRESHED = ("version", "total", "total_text", "subtotal_text", "tax_text", "delivery_label", "shipping_text",
                   "arrives", "has_details", "ship_to", "contact", "payable", "problem", "lines")
# What opening a guest checkout puts on the card (STATE.currency is a Prefab helper, so: pay_currency).
GUEST_OPENED = (*GUEST_REFRESHED, "guest_token", "checkout_id", "pay_currency", "delivery", "has_profile",
                "profile_text", "profile_used", "typing", "g_cards", "review_ready", "authorization", "pay_with_text")
GUEST_FIELDS = {"g_name": "full_name", "g_email": "email", "g_line1": "line1", "g_line2": "line2",
                "g_city": "city", "g_state": "state", "g_zip": "postal_code"}


def guest_state(co: dict) -> dict:
    """A guest checkout's values. Contact details come back masked (city, state and ZIP; the email as
    r***@example.com), so they aren't repeated to the model."""
    dl = co.get("delivery") or {}
    chosen = next((o for o in dl.get("options", []) if o["method"] == dl.get("method")), {})
    addr, guest = co.get("address") or {}, co.get("guest") or {}
    has_details = bool(guest.get("email") and addr)
    codes = {i.get("code") for i in co.get("issues") or []}
    problems = [i for i in co.get("issues") or [] if not (i.get("code") == "NO_ADDRESS" and "NO_GUEST_DETAILS" in codes)]
    return {
        "checkout_id": co["checkout_id"], "version": co["checkout_version"], "total": co["total"],
        "currency": co.get("currency") or "USD", "total_text": money(co["total"]),
        "subtotal_text": money(co["subtotal"]), "tax_text": money(co.get("tax")),
        "delivery_label": f"Delivery: {chosen.get('label', 'Standard')}, arrives {dl.get('date')}",
        "shipping_text": money(co.get("shipping")), "arrives": dl.get("date"),
        "has_details": has_details,
        "ship_to": (f"Ship to: {addr.get('full_name') or guest.get('name') or ''}, {addr.get('city')}, "
                    f"{addr.get('state')} {addr.get('postal_code')}" if has_details
                    else "Ship to: add your details below"),
        "contact": f"Confirmation email to: {mask_email(guest.get('email'))}" if guest.get("email") else "",
        "payable": bool(co.get("ready")) and (co.get("payment_path") or {}).get("route") == "provider_handoff",
        "problem": " ".join(i.get("message") or f"{i.get('name') or 'Item'}: {i.get('code')}" for i in problems),
        "lines": _line_rows(co),
    }


def guest_card_state(co: Optional[dict] = None, token: str = "") -> dict:
    st = guest_state(co) if co else {
        "checkout_id": "", "version": 0, "total": 0.0, "currency": "USD", "total_text": "", "subtotal_text": "",
        "tax_text": "", "delivery_label": "", "shipping_text": "", "arrives": "", "has_details": False,
        "ship_to": "", "contact": "", "payable": False, "problem": "", "lines": []}
    from backend.shop import agent_profile          # the shopper's authorized (fictional demo) profile
    prov = agent_profile.provider() if co else None
    preview = (prov.preview("") or {}) if prov else {}
    offer = {"available": bool(prov), "areas": preview.get("areas") or [],
             "networks": preview.get("payment_networks") or [], "simulated": bool(prov and prov.simulated)}
    return {**st, "guest_open": bool(co), "guest_token": token, "pay_currency": st["currency"],
            "delivery": ((co or {}).get("delivery") or {}).get("method") or "standard",
            "paid": False, "order_ref": "", "handing_off": False, "hosted_url": "", "session_id": "",
            "sent_to": "", "email_note": "", **{k: "" for k in GUEST_FIELDS},
            # profile path: use the saved shipping info, pick a sandbox card, review, approve
            "has_profile": bool(offer.get("available")),
            # before the shopper agrees: city/state and card networks only (no name, street or digits)
            "profile_text": (f"A shipping address in {offer['areas'][0] if offer['areas'] else 'your profile'}"
                             + (f" · {len(offer['networks'])} sandbox card(s)" if offer["networks"] else "")
                             if offer.get("available") else ""),
            "profile_used": False, "typing": False,
            "g_cards": [], "review_ready": False, "authorization": "", "pay_with_text": ""}


def _guest_profile_steps() -> None:
    """The guest's authorized profile: use the saved shipping info, pick a sandbox card, review,
    then Approve & pay (card_place_guest_order: Claude asks the shopper first). No form, no card entry."""
    S = STATE
    with If(~S.profile_used):
        Separator()
        Text("I have your shipping information available", css_class="font-medium")
        Muted(f"{S.profile_text}")
        Muted("From your authorized profile (fictional demo profile). Use it for this order?")
        with Row(gap=2, css_class="flex-wrap"):
            Button("Yes, use it", on_click=CallTool(
                "card_use_guest_profile", arguments={"guest_token": S.guest_token},
                onSuccess=[*_set_from_result(GUEST_REFRESHED), *_set_from_result(("profile_used", "g_cards")),
                           SetState("g_email", RESULT.track_email)],
                onError=failed_toast("Couldn't use your saved details")))
            Button("Enter different details", variant="outline", on_click=SetState("typing", True))
    with If(S.profile_used), If(~S.review_ready):
        Separator()
        Text("How would you like to pay?", css_class="font-medium")
        with Select(name="delivery", value="standard", size="sm"):
            SelectOption(value="standard", label="Standard delivery (free)")
            SelectOption(value="express", label=f"Express delivery (+{money(EXPRESS_PRICE)})")
        with ForEach("g_cards") as c:
            Button(f"{c.label}", variant="outline", on_click=CallTool(
                "card_review_guest_order", arguments={"guest_token": S.guest_token, "payment_method_id": c.id,
                                                      "delivery": S.delivery},
                onSuccess=_set_from_result((*GUEST_REFRESHED, "review_ready", "authorization", "pay_with_text")),
                onError=failed_toast("Couldn't review the order")))
        Muted("Sandbox cards from your profile: ShopSphere and Claude see only the network and last 4 digits. "
              "Choosing one shows the final total to approve.")
    with If(S.review_ready):
        Muted(f"{S.pay_with_text}")


def _guest_profile_pay() -> None:
    S = STATE
    with If(S.review_ready), If(~S.paid):
        Button(f"Approve & pay {S.total_text}", on_click=CallTool(
            "card_place_guest_order", arguments={"guest_token": S.guest_token, "authorization": S.authorization},
            onSuccess=[SetState("paid", True), SetState("order_ref", RESULT.order_id),
                       SetState("sent_to", RESULT.confirmation_sent_to), SetState("arrives", RESULT.arrives),
                       SetState("email_note", ""),
                       ShowToast("Payment approved", variant="success")],
            onError=failed_toast("Payment not completed")))
        Muted("Claude asks you to allow the payment. Then ShopSphere checks this exact order again (a single-use "
              "authorization), runs the simulated card approval and payment, and places the order once.")
        Button("Change card or delivery", variant="ghost", size="sm", on_click=SetState("review_ready", False))


def guest_section() -> None:
    """Items and totals; the shopper's authorized profile (use it, pick a sandbox card, Approve & pay),
    or their own details form (typed here, sent straight to ShopSphere) and a new card on the secure
    payment page; then confirmation -> tracking."""
    S = STATE
    H3("Guest checkout at ShopSphere")
    Muted("No account needed. Your details are used for this order only.")
    with Card():
        with CardContent(), Column(gap=2):
            _line_list()
            Separator()
            for label, key in (("Subtotal", "subtotal_text"), (None, "shipping_text"), ("Tax", "tax_text")):
                with Row(gap=2, css_class="justify-between"):
                    Muted(label if label else f"{S.delivery_label}")
                    Muted(f"{getattr(S, key)}")
            with Row(gap=2, css_class="justify-between"):
                Text("Total", css_class="font-semibold")
                Text(f"{S.total_text}", css_class="font-semibold")
            with If(S.profile_used):
                Muted(f"{S.ship_to}")
            with If(S.has_profile), If(~S.typing), If(~S.paid):
                _guest_profile_steps()
            with If(~S.has_profile | S.typing):
                Muted(f"{S.ship_to}")
                with If(S.contact):
                    Muted(f"{S.contact}")
                Muted("Pay with: a new card on the secure payment page (used once, not saved)")
            with If(~S.paid), If(~S.has_profile | S.typing):
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
                    with Select(name="delivery", value="standard", size="sm"):
                        SelectOption(value="standard", label="Standard delivery (free)")
                        SelectOption(value="express", label=f"Express delivery (+{money(EXPRESS_PRICE)})")
                Button("Save details & update total", variant="outline", size="sm", on_click=CallTool(
                    "update_guest_checkout", arguments={
                        "guest_token": S.guest_token, "delivery": S.delivery,
                        **{arg: getattr(S, k) for k, arg in GUEST_FIELDS.items()}},
                    onSuccess=[*_set_from_result(GUEST_REFRESHED), ShowToast("Details saved", variant="success")],
                    onError=failed_toast("Please check your details")))
                Muted("Your name, email and address go straight to ShopSphere for this order; no account is "
                      "created. Card details go only on the secure payment page, never in the chat.")
        with CardFooter(), Column(gap=2):
            _guest_profile_pay()
            with If(~S.payable), If(~S.paid), If(~S.has_profile | S.typing):
                Muted(f"{S.problem}")
            with If(S.payable), If(~S.paid), If(~S.has_profile | S.typing):
                with If(~S.handing_off):
                    Button(f"Approve & pay {S.total_text}", on_click=CallTool(
                        "start_guest_payment", arguments={
                            "guest_token": S.guest_token, "checkout_version": S.version, "final_amount": S.total,
                            "currency": S.pay_currency},
                        onSuccess=[SetState("hosted_url", RESULT.hosted_url),
                                   SetState("session_id", RESULT.payment_session_id),
                                   SetState("handing_off", True), OpenLink(RESULT.hosted_url)],
                        onError=failed_toast("Couldn't open the secure payment page")))
                    Muted("Next you enter your card on the secure payment page (simulated provider, test cards "
                          "only). Claude never sees it. Nothing is charged until you finish there.")
                with If(S.handing_off):
                    _handoff_buttons(S.hosted_url, CallTool(
                        "finish_guest_payment", arguments={"guest_token": S.guest_token,
                                                           "payment_session_id": S.session_id},
                        onSuccess=[SetState("paid", True), SetState("order_ref", RESULT.order_id),
                                   SetState("sent_to", RESULT.sent_to), SetState("arrives", RESULT.arrives),
                                   SetState("email_note", RESULT.email_note),
                                   ShowToast("Payment approved", variant="success")],
                        onError=failed_toast("Not finished yet")))
            with If(S.paid):
                Text("✓ Your ShopSphere order has been placed", css_class="font-semibold")
                Text(f"Order ID: {S.order_ref}", css_class="font-medium")
                Muted(f"Confirmation sent to: {S.sent_to}")
                Muted(f"Estimated delivery: {S.arrives}")
                Muted(f"{S.email_note}")
                Muted("You can track your order using your Order ID and email address.")
                _track_button({"order_id": S.order_ref, "email": S.g_email})


# ── Signed-in checkout ─────────────────────────────────────────────────────────

# Values update_checkout_choices / open_checkout return (the card keeps them as c_<key>).
CHECKOUT_KEYS = ("checkout_id", "version", "total", "total_text", "subtotal_text", "has_offer", "offer_label",
                 "offer_text", "has_points", "points_label", "points_text", "delivery_label", "shipping_text",
                 "arrives", "tax_text", "ship_to", "pay_with_text", "new_card", "earn_text", "payable", "problem",
                 "lines", "addresses", "pay_options", "points_max", "points_use_label", "pay_currency")


def checkout_state(co: dict) -> dict:
    """A signed-in checkout's values: what the card shows and the choices it offers."""
    path = co.get("payment_path") or {}
    route = path.get("route")
    dl = co.get("delivery") or {}
    chosen = next((o for o in dl.get("options", []) if o["method"] == dl.get("method")), {})
    points = co.get("points") or {}
    card = (path.get("card") or {}).get("display") or path.get("label") or "no saved card"
    addr_id = (co.get("address") or {}).get("address_id")
    paying = co.get("pay_with") if co.get("pay_with") in ("wallet", "provider") else \
        (co.get("payment_method") or {}).get("payment_method_id")
    options = [(c["payment_method_id"], f"Pay with {c.get('issuer_display') + ' ' if c.get('issuer_display') else ''}"
                                        f"{c['display']}") for c in co.get("saved_payment_methods") or []]
    if (co.get("wallet") or {}).get("enough") and co.get("saved_payment_methods"):
        options.append(("wallet", f"Pay with ShopSphere Wallet ({money(co['wallet'].get('balance'))})"))
    if co.get("provider_option"):
        options.append(("provider", "Pay with a new card (secure payment page)"))
    max_points = int(points.get("max_usable") or 0)
    return {
        "checkout_id": co["checkout_id"], "version": co["checkout_version"], "total": co["total"],
        "currency": co.get("currency") or "USD", "pay_currency": co.get("currency") or "USD",
        "total_text": money(co["total"]), "subtotal_text": money(co["subtotal"]),
        "has_offer": bool(co.get("offer_discount")),
        "offer_label": f"Chase offer: {(co.get('offer') or {}).get('title', 'offer')}",
        "offer_text": "−" + money(co.get("offer_discount")),
        "has_points": bool(co.get("points_discount")),
        "points_label": f"Points ({points.get('used', 0)})", "points_text": "−" + money(co.get("points_discount")),
        "delivery_label": f"Delivery: {chosen.get('label', 'Standard')}, arrives {dl.get('date')}",
        "shipping_text": money(co.get("shipping")), "arrives": dl.get("date"),
        "tax_text": money(co.get("tax")),
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
        "lines": _line_rows(co),
        "addresses": [{"id": a["address_id"], "label": ("✓ " if a["address_id"] == addr_id else "")
                       + f"Ship to {a.get('label') or 'address'}: {a['display']}"}
                      for a in co.get("saved_addresses") or []],
        "pay_options": [{"id": i, "label": ("✓ " if i == paying else "") + label} for i, label in options],
        "points_max": max_points,
        "points_use_label": (f"Use {max_points:,} points (−{money(max_points * points.get('value_per_point', 0.01))})"
                             if max_points else ""),
    }


def checkout_card_state(co: Optional[dict] = None) -> dict:
    st = checkout_state(co) if co else {k: ([] if k in ("lines", "addresses", "pay_options") else
                                           0 if k in ("version", "total", "points_max") else
                                           False if k in ("has_offer", "has_points", "new_card", "payable") else
                                           "USD" if k == "pay_currency" else "") for k in CHECKOUT_KEYS}
    return {"co_open": bool(co), **{f"c_{k}": st[k] for k in CHECKOUT_KEYS},
            "c_confirming": False, "c_paid": False, "c_order_ref": "", "c_handing_off": False, "c_hosted_url": "",
            "c_session_id": ""}


def checkout_section() -> None:
    """The signed-in review: items, totals and choices (address, card, delivery, points) that
    update in place; then Approve & pay -> simulated passkey (or the secure page for a new card)."""
    S = STATE
    refresh = [*_set_from_result(CHECKOUT_KEYS, "c_")]

    def choose(**args):
        return CallTool("update_checkout_choices", arguments={"checkout_id": S.c_checkout_id, **args},
                        onSuccess=[*refresh, ShowToast("Order updated", variant="success")],
                        onError=failed_toast("Couldn't update the order"))

    H3("Review your ShopSphere order")
    with Card():
        with CardContent(), Column(gap=2):
            _line_list("c_lines")
            Separator()
            with Row(gap=2, css_class="justify-between"):
                Muted("Subtotal")
                Muted(f"{S.c_subtotal_text}")
            with If(S.c_has_offer):
                with Row(gap=2, css_class="justify-between"):
                    Muted(f"{S.c_offer_label}")
                    Muted(f"{S.c_offer_text}")
            with If(S.c_has_points):
                with Row(gap=2, css_class="justify-between"):
                    Muted(f"{S.c_points_label}")
                    Muted(f"{S.c_points_text}")
            with Row(gap=2, css_class="justify-between"):
                Muted(f"{S.c_delivery_label}")
                Muted(f"{S.c_shipping_text}")
            with Row(gap=2, css_class="justify-between"):
                Muted("Tax")
                Muted(f"{S.c_tax_text}")
            with Row(gap=2, css_class="justify-between"):
                Text("Total", css_class="font-semibold")
                Text(f"{S.c_total_text}", css_class="font-semibold")
            Muted(f"{S.c_ship_to}")
            Muted(f"{S.c_pay_with_text}")
            Muted(f"{S.c_earn_text}")
            with If(~S.c_paid), If(~S.c_handing_off), If(~S.c_confirming):
                Separator()
                Text("Change your order", css_class="font-medium")
                with ForEach("c_addresses") as a:
                    Button(f"{a.label}", variant="ghost", size="sm", on_click=choose(address_id=a.id))
                with ForEach("c_pay_options") as p:
                    Button(f"{p.label}", variant="ghost", size="sm", on_click=choose(pay_with=p.id))
                with Row(gap=2, css_class="flex-wrap"):
                    Button("Standard delivery (free)", variant="outline", size="sm",
                           on_click=choose(delivery="standard"))
                    Button(f"Express delivery (+{money(EXPRESS_PRICE)})", variant="outline", size="sm",
                           on_click=choose(delivery="express"))
                with If(S.c_points_max):
                    with Row(gap=2, css_class="flex-wrap"):
                        Button(f"{S.c_points_use_label}", variant="outline", size="sm", on_click=choose(use_points="yes"))
                        Button("Don't use points", variant="outline", size="sm", on_click=choose(use_points="no"))
                Muted("New card? Choose \"Pay with a new card\". You type it only on the secure payment page (and can "
                      "save it there), never in the chat. New addresses: your ShopSphere account.")
        with CardFooter(), Column(gap=2):
            with If(~S.c_payable), If(~S.c_paid):
                Muted(f"This order can't be paid here yet. {S.c_problem}")
            with If(S.c_payable), If(~S.c_paid):
                scope = {"checkout_id": S.c_checkout_id, "checkout_version": S.c_version, "final_amount": S.c_total,
                         "currency": S.c_pay_currency}
                with If(S.c_new_card):
                    with If(~S.c_handing_off):
                        Button(f"Approve & pay {S.c_total_text}", on_click=CallTool(
                            "start_new_card_payment", arguments=scope,
                            onSuccess=[SetState("c_hosted_url", RESULT.hosted_url),
                                       SetState("c_session_id", RESULT.payment_session_id),
                                       SetState("c_handing_off", True), OpenLink(RESULT.hosted_url)],
                            onError=failed_toast("Couldn't open the secure payment page")))
                        Muted("Next you enter your card on the secure payment page (simulated provider, test cards "
                              "only). Claude never sees it. Nothing is charged until you finish there.")
                    with If(S.c_handing_off):
                        _handoff_buttons(S.c_hosted_url, CallTool(
                            "finish_new_card_payment", arguments={"checkout_id": S.c_checkout_id,
                                                                  "payment_session_id": S.c_session_id},
                            onSuccess=[SetState("c_paid", True), SetState("c_order_ref", RESULT.order_id),
                                       SetState("c_arrives", RESULT.arrives),
                                       ShowToast("Payment approved", variant="success")],
                            onError=failed_toast("Not finished yet")), save_hint=True)
                with If(~S.c_new_card):
                    with If(~S.c_confirming):
                        Button(f"Approve & pay {S.c_total_text}", on_click=SetState("c_confirming", True))
                        Muted("Nothing is charged until you approve. Claude can't press this button.")
                    with If(S.c_confirming):
                        Text("Confirm it's you", css_class="font-medium")
                        Muted("Simulated passkey (demo, not a real passkey; your card issuer isn't contacted).")
                        with Row(gap=2):
                            Button("Confirm with passkey", variant="success", on_click=CallTool(
                                "approve_and_pay", arguments=scope,
                                onSuccess=[SetState("c_paid", True), SetState("c_order_ref", RESULT.order_id),
                                           SetState("c_arrives", RESULT.arrives),
                                           ShowToast("Payment approved", variant="success")],
                                onError=[SetState("c_confirming", False), failed_toast("Payment not completed")]))
                            Button("Cancel", variant="outline", on_click=SetState("c_confirming", False))
            with If(S.c_paid):
                Text(f"✓ Order {S.c_order_ref} confirmed", css_class="font-semibold")
                Muted(f"Arrives {S.c_arrives}. A confirmation email is on its way.")
                _track_button({"order_id": S.c_order_ref})


# ── Shared pieces ──────────────────────────────────────────────────────────────

def _line_rows(co: dict) -> list[dict]:
    return [{"label": f"{ln['name']} ({variant_label(ln.get('size'), ln.get('color'))}) × {ln['quantity']}",
             "amount": money(ln["unit_price"] * ln["quantity"])} for ln in co.get("lines") or []]


def _line_list(key: str = "lines") -> None:
    with ForEach(key) as ln:
        with Row(gap=2, css_class="justify-between"):
            Text(f"{ln.label}")
            Text(f"{ln.amount}")


def _handoff_buttons(hosted_url, finish: CallTool, save_hint: bool = False) -> None:
    Text("Finish on the secure payment page", css_class="font-medium")
    Muted("It opened in a new browser tab. Enter the card there"
          + (" (tick Save to keep it for next time)" if save_hint else "") + ", then come back here.")
    with Row(gap=2):
        Button("Open secure payment page", variant="outline", on_click=OpenLink(hosted_url))
        Button("I've finished paying", variant="success", on_click=finish)


def _open_tracking(args: dict) -> CallTool:
    """Load an order's tracking and show it in this card (with a Back button)."""
    return CallTool("order_status", arguments=args,
                    onSuccess=[*_set_from_result(TRACK_KEYS), SetState("track_open", True),
                               SetState("track_can_back", True)],
                    onError=failed_toast("Couldn't load tracking"))


def _track_button(args: dict) -> None:
    Button("Track my order", variant="outline", size="sm", on_click=_open_tracking(args))


def shop_sections() -> None:
    """The cart, guest checkout, signed-in checkout, order list and tracking screens; the state
    decides which shows."""
    S = STATE
    with If(S.cart_open & ~S.guest_open & ~S.co_open & ~S.track_open):
        cart_section()
    with If(S.guest_open & ~S.track_open):
        guest_section()
    with If(S.co_open & ~S.track_open):
        checkout_section()
    with If(S.orders_list & ~S.track_open):
        orders_section()
    with If(S.track_open):
        tracking_section()


def shop_state(*, cart: Optional[dict] = None, guest: Optional[dict] = None,
               checkout: Optional[dict] = None, tracking: Optional[dict] = None,
               orders: Optional[list] = None, has_products: bool = False) -> dict:
    """A card's full starting state: every screen's values, and which screen is open."""
    return {"has_products": has_products, "cart_open": False,
            **(cart or cart_state(None, None, False)),
            **(guest or guest_card_state()),
            **(checkout or checkout_card_state()),
            "track_open": bool(tracking), "track_can_back": False, **(tracking or tracking_state(None)),
            "orders_list": orders is not None, "o_orders": orders or [], "o_empty": orders == []}


# ── Order tracking ─────────────────────────────────────────────────────────────

# The shipment steps (backend/shop/shipping.py), each with its icon on the timeline.
TRACK_STEPS = (("confirmed", "Order confirmed", "clipboard-check"),
               ("payment_completed", "Payment completed", "credit-card"),
               ("processing", "Processing", "package-search"),
               ("packed", "Packed", "package"),
               ("shipped", "Shipped", "truck"),
               ("out_for_delivery", "Out for delivery", "map-pin"),
               ("delivered", "Delivered", "house"))
_STEP_FIELDS = ("done", "now", "when", "note")
# What order_status returns and the tracking screen shows (all prefixed t_).
TRACK_KEYS = ("t_order_id", "t_email", "t_badge", "t_headline", "t_sub", "t_percent", "t_map", "t_carrier",
              "t_tracking_id", "t_lines", "t_totals", "t_total_text", "t_paid_with", "t_ship_to", "t_delivered",
              "t_updated", *(f"t_{f}_{k}" for k, _, _ in TRACK_STEPS for f in _STEP_FIELDS))


def _when(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        at = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)      # stored times are UTC


def _stamp(value) -> str:
    """'Thu, Nov 12 · 6:05 AM UTC'"""
    at = _when(value)
    if not at:
        return ""
    at = at.astimezone(timezone.utc)
    return f"{at:%a, %b} {at.day} · {at.hour % 12 or 12}:{at:%M} {'AM' if at.hour < 12 else 'PM'} UTC"


def _day(value, weekday: str = "%A") -> str:
    """'Monday, Nov 16'"""
    try:
        d = date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        d = None
    return f"{d.strftime(weekday)}, {d:%b} {d.day}" if d else ""


def _tracking_basics(view: dict) -> dict:
    sh = view.get("shipment") or {}
    keys = [k for k, _, _ in TRACK_STEPS]
    status = sh.get("status") if sh.get("status") in keys else "processing"
    ship_to = view.get("ship_to") or {}
    eta = _day(sh.get("estimated_delivery") or view.get("delivery_date"))
    events = {s.get("key"): s.get("at") for s in sh.get("steps") or []}
    where = ", ".join(x for x in (ship_to.get("city"), ship_to.get("state")) if x)
    if status == "delivered":
        headline = f"Delivered {_day(events.get('delivered'))}".strip()
    elif status == "out_for_delivery":
        headline = "Out for delivery"
    else:
        headline = f"Arriving {eta}" if eta else "On its way"
    count = sum(int(ln.get("quantity") or 1) for ln in view.get("lines") or [])
    return {"status": status, "reached": keys.index(status), "eta": eta, "events": events, "where": where,
            "city": ship_to.get("city"), "state": ship_to.get("state"), "headline": headline,
            "badge": sh.get("status_label") or "Processing", "count": count,
            "percent": round(keys.index(status) / (len(keys) - 1) * 100)}


def tracking_state(view: Optional[dict], email: str = "",
                   photo: Callable[[Optional[str], int], Optional[str]] = lambda u, px: None) -> dict:
    """The tracking screen's values for one order (order_service.tracking_view): headline, progress,
    the route map, the timeline with times, the items with photos and the totals. Only the city and
    state of the address; the card as brand + last 4."""
    if not view:
        empty = {k: "" for k in TRACK_KEYS}
        empty.update(t_percent=0, t_lines=[], t_totals=[], t_delivered=False,
                     **{f"t_{f}_{k}": False for k, _, _ in TRACK_STEPS for f in ("done", "now")})
        return empty
    b = _tracking_basics(view)
    sh = view.get("shipment") or {}
    paid_with = (view.get("payment") or {}).get("display") or ""
    tracking_id = sh.get("tracking_number") or ""
    notes = {"confirmed": "We received your order.",
             "payment_completed": f"Payment approved{' · ' + paid_with if paid_with else ''}.",
             "processing": "Being picked and prepared at the ShopSphere fulfilment center.",
             "packed": "Packed and labelled, waiting for the carrier.",
             "shipped": f"Handed to the carrier{' · Tracking ID ' + tracking_id if tracking_id else ''}.",
             "out_for_delivery": f"On the delivery van{' in ' + b['city'] if b['city'] else ''}.",
             "delivered": f"Delivered{' to ' + b['where'] if b['where'] else ''}."}
    state = {
        "t_order_id": view.get("order_id") or "", "t_email": email or "",
        "t_badge": b["badge"], "t_headline": b["headline"],
        "t_sub": " · ".join(x for x in (
            f"Placed {_day(view.get('created_at'), '%a')}" if view.get("created_at") else "",
            f"{(view.get('delivery_method') or 'standard').title()} delivery",
            f"{b['count']} item{'s' if b['count'] != 1 else ''}") if x),
        "t_percent": b["percent"],
        "t_map": route_map(b["status"], b["city"], b["state"]),
        "t_carrier": sh.get("carrier") or "Assigned when your parcel ships",
        "t_tracking_id": tracking_id or "Assigned when your parcel ships",
        "t_lines": [{"label": ln.get("name") or "", "amount": money(ln.get("line_total")),
                     "sub": " · ".join(x for x in (variant_label(ln.get("size"), ln.get("color")),
                                                   f"Qty {ln.get('quantity') or 1}") if x),
                     "photo": photo(ln.get("image_url"), 112) or ""} for ln in view.get("lines") or []],
        "t_totals": [{"label": "Subtotal", "amount": money(view.get("subtotal"))},
                     {"label": "Shipping", "amount": money(view.get("shipping")) if view.get("shipping") else "Free"},
                     {"label": "Tax", "amount": money(view.get("tax"))}],
        "t_total_text": money(view.get("total")),
        "t_paid_with": paid_with or "Not shown", "t_ship_to": b["where"] or "Your shipping address",
        "t_delivered": b["status"] == "delivered",
        "t_updated": _stamp(datetime.now(timezone.utc).isoformat()),
    }
    for i, (key, _, _) in enumerate(TRACK_STEPS):
        done = i <= b["reached"]
        state[f"t_done_{key}"] = done
        state[f"t_now_{key}"] = i == b["reached"] and key != "delivered"
        state[f"t_when_{key}"] = (_stamp(b["events"].get(key)) if done else
                                  f"Expected {b['eta']}" if key == "delivered" and b["eta"] else
                                  "Up next" if i == b["reached"] + 1 else "")
        state[f"t_note_{key}"] = notes[key] if done else ""
    return state


def order_row(view: dict, photo: Callable[[Optional[str], int], Optional[str]] = lambda u, px: None) -> dict:
    """One order in the signed-in shopper's order list: photo, status, progress, Track."""
    b = _tracking_basics(view)
    lines = view.get("lines") or []
    first = lines[0] if lines else {}
    names = first.get("name") or "Order"
    if len(lines) > 1:
        names += f" + {len(lines) - 1} more"
    return {"order_id": view.get("order_id") or "", "badge": b["badge"], "headline": b["headline"],
            "pct": b["percent"], "items": names, "photo": photo(first.get("image_url"), 112) or "",
            "sub": " · ".join(x for x in (f"Placed {_day(view.get('created_at'), '%a')}" if view.get("created_at")
                                          else "", money(view.get("total"))) if x)}


def tracking_section() -> None:
    """One order's tracking: status and arrival, progress, the route map, the shipment timeline with
    times, carrier and tracking ID, the items with photos, totals; Refresh and a demo control."""
    S = STATE
    refresh = _set_from_result(TRACK_KEYS)
    lookup = {"order_id": S.t_order_id, "email": S.t_email}
    with Card():
        with CardContent(), Column(gap=4):
            with Row(gap=2, align="center", css_class="justify-between"):
                with Column(gap=0):
                    Muted("ShopSphere order")
                    H3(f"{S.t_order_id}")
                Badge(f"{S.t_badge}", variant="secondary")
            with Column(gap=1):
                Text(f"{S.t_headline}", css_class="text-xl font-semibold")
                Muted(f"{S.t_sub}")
            with Column(gap=1):
                Progress(value=f"{S.t_percent}", max=100, variant="success")
                with Row(css_class="justify-between"):
                    for label in ("Ordered", "Processing", "Shipped", "Delivered"):
                        Muted(label)
            with If(S.t_map):
                Image(src=f"{S.t_map}", alt="Delivery route map: where the parcel is now",
                      css_class="w-full h-auto rounded-lg border")
            with Grid(columns={"default": 1, "sm": 2}, gap=3):
                for icon, title, value in (("truck", "Carrier", S.t_carrier),
                                           ("scan-barcode", "Tracking ID", S.t_tracking_id)):
                    with Row(gap=2, align="center"):
                        Icon(icon, css_class="text-muted-foreground")
                        with Column(gap=0):
                            Muted(title)
                            Text(f"{value}", css_class="font-medium")
            Separator()
            Text("Shipment progress", css_class="font-semibold")
            with Column(gap=3):
                for key, label, icon in TRACK_STEPS:
                    done, now = getattr(S, f"t_done_{key}"), getattr(S, f"t_now_{key}")
                    when, note = getattr(S, f"t_when_{key}"), getattr(S, f"t_note_{key}")
                    with Row(gap=3, align="start"):
                        with If(done):
                            Icon(icon, css_class="text-green-600 mt-0.5")
                        with If(~done):
                            Icon(icon, css_class="text-muted-foreground opacity-50 mt-0.5")
                        with Column(gap=0, css_class="flex-1"):
                            with Row(gap=2, align="center"):
                                with If(done):
                                    Text(label, css_class="font-medium")
                                with If(~done):
                                    Text(label, css_class="text-muted-foreground")
                                with If(now):
                                    Badge("Now", variant="success")
                            with If(note):
                                Muted(f"{note}")
                            with If(when):
                                Muted(f"{when}")
            Separator()
            Text("In this package", css_class="font-semibold")
            with ForEach("t_lines") as ln:
                with Row(gap=3, align="center"):
                    with If(ln.photo):
                        Image(src=f"{ln.photo}", alt=f"{ln.label}",
                              css_class="w-16 h-16 object-contain bg-white rounded-md border")
                    with Column(gap=0, css_class="flex-1"):
                        Text(f"{ln.label}", css_class="font-medium")
                        Muted(f"{ln.sub}")
                    Text(f"{ln.amount}")
            with Column(gap=1):
                with ForEach("t_totals") as row:
                    with Row(css_class="justify-between"):
                        Muted(f"{row.label}")
                        Muted(f"{row.amount}")
                with Row(css_class="justify-between"):
                    Text("Total", css_class="font-semibold")
                    Text(f"{S.t_total_text}", css_class="font-semibold")
            with Grid(columns={"default": 1, "sm": 2}, gap=3):
                for icon, title, value in (("map-pin", "Shipping to", S.t_ship_to),
                                           ("credit-card", "Paid with", S.t_paid_with)):
                    with Row(gap=2, align="center"):
                        Icon(icon, css_class="text-muted-foreground")
                        with Column(gap=0):
                            Muted(title)
                            Text(f"{value}", css_class="font-medium")
        with CardFooter(), Column(gap=2):
            with Row(gap=2, css_class="flex-wrap"):
                Button("Refresh status", variant="outline", size="sm", on_click=CallTool(
                    "order_status", arguments=lookup,
                    onSuccess=[*refresh, ShowToast("Tracking updated", variant="success")],
                    onError=failed_toast("Couldn't refresh tracking")))
                with If(~S.t_delivered):
                    Button("Simulate next step (demo)", variant="ghost", size="sm", on_click=CallTool(
                        "advance_demo_shipment", arguments=lookup,
                        onSuccess=[*refresh, ShowToast("Shipment moved to the next step", variant="success")],
                        onError=failed_toast("Couldn't move the shipment")))
                with If(S.track_can_back):
                    Button("Back", variant="ghost", size="sm", on_click=SetState("track_open", False))
            Muted(f"Updated {S.t_updated}. Shipping in this demo is simulated: no real carrier or vehicle "
                  "location, and the map shows your city only, never your street address.")


def orders_section() -> None:
    """A signed-in shopper's recent orders, each with photo, status and progress; Track opens the
    full tracking for that order in this card."""
    S = STATE
    H3("Your ShopSphere orders")
    with If(S.o_empty):
        Muted("No orders yet. Ask me to find something!")
    with ForEach("o_orders") as o:
        with Card():
            with CardContent(), Row(gap=3, align="center"):
                with If(o.photo):
                    Image(src=f"{o.photo}", alt=f"{o.items}",
                          css_class="w-16 h-16 object-contain bg-white rounded-md border")
                with Column(gap=1, css_class="flex-1"):
                    with Row(gap=2, align="center"):
                        Text(f"{o.order_id}", css_class="font-semibold")
                        Badge(f"{o.badge}", variant="secondary")
                    Text(f"{o.headline}", css_class="font-medium")
                    Muted(f"{o.items} · {o.sub}")
                    Progress(value=f"{o.pct}", max=100, variant="success", size="sm")
                Button("Track", variant="outline", size="sm", on_click=_open_tracking({"order_id": o.order_id}))
