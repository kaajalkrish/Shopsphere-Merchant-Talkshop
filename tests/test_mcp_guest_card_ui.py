"""
Interactive UI regression tests for the ShopSphere card inside Claude (Prefab MCP App).

`CardDriver` plays the part of the Prefab renderer inside Claude: it reads the card JSON the
server returned (view tree + state), shows only what the card's conditions allow, fills inputs
into their bound state, and on a click runs exactly the actions the button declares (toolCall ->
tools/call over the MCP HTTP endpoint, then setState / openLink / showToast with `$result`).
A 401 from the connector (sign-in needed) is what Claude turns into its sign-in window; here it is
recorded and the button's onError runs. So these tests fail if a button is wired to the wrong
tool, sends a chat message, loses state, or never reveals the next step.
"""
import json
import re

from backend.db.schema import Order
from backend.db.session_utils import get_session
from backend.mcp import auth as mcp_auth
from tests.test_mcp_shop_flow import _on_secure_page, call, client, rpc, sign_in  # noqa: F401 (fixture)

_TEMPLATE = re.compile(r"\{\{\s*(.*?)\s*\}\}")


class _Obj(dict):
    """Dotted access for template expressions (state.x, $result.y, loop item.z)."""
    def __getattr__(self, name):
        v = self.get(name)
        return _Obj(v) if isinstance(v, dict) else v


class CardDriver:
    def __init__(self, client, structured: dict, token: str | None = None):
        assert structured.get("$prefab") and "view" in structured, "not a Prefab card"
        self.client, self.view, self.token = client, structured["view"], token
        self.state = dict(structured.get("state") or {})
        self.tool_calls: list[str] = []
        self.links: list[str] = []
        self.toasts: list[str] = []
        self.sign_in_requests = 0

    # ── expressions ──
    def _eval(self, expr: str, scope: dict):
        py = expr.replace("$result", "_result").replace("$error", "_error").replace("$item", "_item")
        py = re.sub(r"!(?!=)", " not ", py).replace("&&", " and ").replace("||", " or ")
        names = {k: (_Obj(v) if isinstance(v, dict) else v) for k, v in {**self.state, **scope}.items()}
        try:
            return eval(py, {"__builtins__": {}}, names)            # test-only: the card's own expressions
        except NameError:
            return None

    def resolve(self, value, scope: dict | None = None):
        scope = scope or {}
        if isinstance(value, dict):
            return {k: self.resolve(v, scope) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v, scope) for v in value]
        if not isinstance(value, str) or "{{" not in value:
            return value
        whole = _TEMPLATE.fullmatch(value.strip())
        if whole and "{{" not in whole.group(1):                    # "{{ x }}" keeps the value's type
            return self._eval(whole.group(1), scope)
        return _TEMPLATE.sub(lambda m: "" if (v := self._eval(m.group(1), scope)) is None else str(v), value)

    # ── what is on screen ──
    def visible(self) -> list[dict]:
        out: list[dict] = []
        self._walk(self.view, {}, out)
        return out

    def _walk(self, node, scope: dict, out: list) -> None:
        if isinstance(node, list):
            for n in node:
                self._walk(n, scope, out)
            return
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        if kind == "Condition":
            for case in node.get("cases") or []:
                if "when" not in case or self.resolve(case["when"], scope):
                    self._walk(case.get("children"), scope, out)
                    break
            return
        if kind == "ForEach":
            key = node["key"]
            items = self.resolve(key if "{{" in key else "{{ " + key + " }}", scope)
            for i, item in enumerate(items or []):
                inner = dict(scope, _item=item)
                for name, expr in (node.get("let") or {}).items():
                    inner[name] = _Obj(item) if "$item" in expr and isinstance(item, dict) else \
                        item if "$item" in expr else i
                self._walk(node.get("children"), inner, out)
            return
        out.append({"node": node, "scope": scope,
                    "text": self.resolve(node.get("content") or node.get("label") or "", scope)})
        self._walk(node.get("children"), scope, out)

    def texts(self) -> str:
        return "\n".join(str(v["text"]) for v in self.visible() if v["text"])

    def inputs(self) -> set[str]:
        return {v["node"]["name"] for v in self.visible() if v["node"].get("type") in ("Input", "Select")}

    def buttons(self) -> list[str]:
        return [str(v["text"]) for v in self.visible() if v["node"].get("type") == "Button"]

    def button(self, label_start: str) -> dict:
        found = [v for v in self.visible() if v["node"].get("type") == "Button" and str(v["text"]).startswith(label_start)]
        assert found, f"no visible button {label_start!r}; visible buttons: {self.buttons()}"
        return found[0]

    # ── the shopper's actions ──
    def fill(self, **fields) -> None:
        shown = self.inputs()
        for name, value in fields.items():
            assert name in shown, f"input {name} isn't on screen"
            self.state[name] = value                               # an input writes its bound state key

    def click(self, label_start: str) -> None:
        b = self.button(label_start)
        self._run(b["node"].get("onClick"), dict(b["scope"]))

    def _run(self, actions, scope: dict) -> bool:
        for a in actions if isinstance(actions, list) else [actions] if actions else []:
            kind = a.get("action")
            if kind == "toolCall":
                args = self.resolve(a.get("arguments") or {}, scope)
                self.tool_calls.append(a["tool"])
                r, msg = rpc(self.client, "tools/call", {"name": a["tool"], "arguments": args}, self.token)
                if r.status_code == 401:                          # Claude would open ShopSphere's sign-in
                    assert "resource_metadata" in r.headers["www-authenticate"]
                    self.sign_in_requests += 1
                    self._run(a.get("onError"), dict(scope, _error="Sign in needed"))
                    return False
                assert r.status_code == 200, f"{a['tool']}: HTTP {r.status_code} {r.text[:200]}"
                res = msg["result"]
                if res.get("isError"):
                    self._run(a.get("onError"), dict(scope, _error=res["content"][0]["text"]))
                    return False
                result = res.get("structuredContent")
                if result is None:
                    result = json.loads(res["content"][0]["text"])
                elif a.get("unwrapResult") and "result" in result:
                    result = result["result"]
                if not self._run(a.get("onSuccess"), dict(scope, _result=_Obj(result))):
                    return False
            elif kind == "setState":
                self.state[a["key"]] = self.resolve(a["value"], scope)
            elif kind == "openLink":
                self.links.append(self.resolve(a.get("url") or a.get("href"), scope))
            elif kind == "showToast":
                self.toasts.append(str(self.resolve(a.get("description") or a.get("message"), scope)))
            else:
                raise AssertionError(f"unexpected card action {kind!r} (cards must not send chat messages)")
        return True


def card_of(client, tool: str, token: str | None = None, **args) -> tuple[dict, dict]:
    r, msg = rpc(client, "tools/call", {"name": tool, "arguments": args}, token)
    assert r.status_code == 200, r.text
    res = msg["result"]
    assert not res.get("isError"), res["content"][0]["text"]
    return json.loads(res["content"][0]["text"]), res["structuredContent"]


def no_chat_messages_or_helper_clashes(structured: dict) -> None:
    """Cards must never send chat messages (Claude ignores them), and no state name may clash with a
    Prefab helper (STATE.currency is one: the card silently broke)."""
    from prefab_ui.rx import RESULT, STATE
    assert '"sendMessage"' not in json.dumps(structured)
    clashes = [k for k in structured["state"] if callable(getattr(STATE, k)) or callable(getattr(RESULT, k))]
    assert clashes in ([], ["currency"]), clashes                  # "currency" is kept but never read by the card
    # list items too ({{ _loop.percent }} is Prefab's percent formatter, not the item's value)
    items = [k for v in structured["state"].values() if isinstance(v, list)
             for item in v if isinstance(item, dict) for k in item]
    assert not [k for k in items if callable(getattr(STATE, k))], sorted(set(items))
    assert "<bound method" not in json.dumps(structured)
    assert '"{{ currency }}"' not in json.dumps(structured["view"]) and "$result.currency" not in json.dumps(structured)


def images(ui: CardDriver) -> list[str]:
    return [str(ui.resolve(v["node"].get("src"), v["scope"])) for v in ui.visible() if v["node"].get("type") == "Image"]


def check_tracking_screen(ui: CardDriver, order_id: str, back_to: str | None = None) -> None:
    """The tracking screen: headline + arrival, progress, route map, the timeline with times, items with
    photos and totals, all in this card; the demo control moves the shipment; Refresh; Back."""
    shown = ui.texts()
    assert ui.state["track_open"] and ui.state["t_order_id"] == order_id and order_id in shown
    assert ui.state["t_headline"].startswith("Arriving ") and ui.state["t_headline"] in shown
    assert ui.state["t_badge"] == "Processing" and ui.state["t_percent"] == 33
    assert any(src.startswith("data:image/svg+xml;base64,") for src in images(ui))        # the route map
    for step in ("Order confirmed", "Payment completed", "Processing", "Packed", "Shipped", "Out for delivery",
                 "Delivered", "Shipment progress", "In this package", "Total", "Shipping to", "Paid with"):
        assert step in shown, step
    assert "Being picked and prepared at the ShopSphere fulfilment center." in shown and "Up next" in shown
    assert re.search(r"[A-Z][a-z]{2}, [A-Z][a-z]{2} \d{1,2} · \d{1,2}:\d{2} [AP]M UTC", shown)  # step times
    assert ui.state["t_when_delivered"].startswith("Expected ") and shown.count("Now") == 1
    assert ui.state["t_tracking_id"] == "Assigned when your parcel ships"
    assert ui.state["t_lines"] and ui.state["t_lines"][0]["label"] in shown
    assert all(ln["photo"].startswith("data:image/jpeg") for ln in ui.state["t_lines"])  # product photos
    assert "Simulated" in shown or "simulated" in shown
    ui.click("Simulate next step (demo)")
    assert ui.tool_calls[-1] == "advance_demo_shipment" and ui.state["t_badge"] == "Packed"
    ui.click("Simulate next step (demo)")
    shown = ui.texts()
    assert ui.state["t_badge"] == "Shipped" and re.fullmatch(r"TRK\d{6}", ui.state["t_tracking_id"])
    assert f"Handed to the carrier · Tracking ID {ui.state['t_tracking_id']}." in shown
    ui.click("Refresh status")
    assert ui.tool_calls[-1] == "order_status" and ui.toasts[-1] == "Tracking updated"
    if back_to:
        ui.click("Back")
        assert not ui.state["track_open"] and back_to in ui.texts() and "Shipment progress" not in ui.texts()


def _empty_cart(client, token):
    for ln in call(client, "show_cart", token)["items"]:
        rpc(client, "tools/call", {"name": "update_cart_line", "arguments": {"line_id": ln["line_id"], "quantity": 0}},
            token)


def _guest_pays_in_the_card(client, ui: CardDriver) -> str:
    """From the guest form to the confirmation and tracking, all on one card (the shopper chose to
    type different details instead of their authorized profile)."""
    assert "I have your shipping information available" in ui.texts()
    assert "g_name" not in ui.inputs()                              # no form until they ask for it
    ui.click("Enter different details")
    shown = ui.texts()
    assert "Guest checkout at ShopSphere" in shown and "Add your name, email and shipping address." in shown
    assert {"g_name", "g_email", "g_line1", "g_line2", "g_city", "g_state", "g_zip", "delivery"} <= ui.inputs()
    assert not [b for b in ui.buttons() if b.startswith("Approve & pay")]           # not payable yet
    ui.fill(g_name="Pat Guest", g_email="not-an-email", g_line1="500 Congress Ave", g_city="Austin",
            g_state="TX", g_zip="78701")
    ui.click("Save details & update total")
    assert any("valid email" in t for t in ui.toasts) and not ui.state["has_details"]
    ui.fill(g_email="pat.card@guestmail.net", delivery="express")
    ui.click("Save details & update total")
    shown = ui.texts()
    assert "Ship to: Pat Guest, Austin, TX 78701" in shown and "Confirmation email to: p***@guestmail.net" in shown
    assert "Delivery: Express" in shown and ui.state["shipping_text"] == "$9.99" and ui.state["payable"]
    ui.click(f"Approve & pay {ui.state['total_text']}")
    assert ui.tool_calls[-1] == "start_guest_payment" and "/psp/hosted/" in ui.links[-1]
    assert "Finish on the secure payment page" in ui.texts()
    ui.click("I've finished paying")                                # too early: nothing happens
    assert "isn't finished" in ui.toasts[-1] and not ui.state["paid"]
    assert "Payment verified" in _on_secure_page(client, ui.links[-1], save=True)   # the card, on the provider's page
    ui.click("I've finished paying")
    shown = ui.texts()
    order_id = ui.state["order_ref"]
    assert ui.state["paid"] and "✓ Your ShopSphere order has been placed" in shown
    assert re.fullmatch(r"SS-\d{5}", order_id) and f"Order ID: {order_id}" in shown
    assert "Confirmation sent to: p***@guestmail.net" in shown and f"Estimated delivery: {ui.state['arrives']}" in shown
    assert "Save details & update total" not in ui.buttons()
    ui.click("Track my order")                                      # in the card, with the checkout email
    check_tracking_screen(ui, order_id, back_to="✓ Your ShopSphere order has been placed")
    with get_session() as db:
        orders = db.query(Order).filter(Order.display_id == order_id).all()
        assert len(orders) == 1 and orders[0].guest_email == "pat.card@guestmail.net"
    return order_id


# ── guest ──────────────────────────────────────────────────────────────────────

def test_product_card_add_view_cart_guest_checkout_pay_and_track_all_in_one_card(client):
    found, structured = card_of(client, "search_shopsphere", query="running shoes", size="10")
    no_chat_messages_or_helper_clashes(structured)
    ui = CardDriver(client, structured)
    assert "ShopSphere picks in size 10" in ui.texts() and "View cart & checkout" not in ui.buttons()
    ui.click("Details")                                             # in place, no chat message
    assert found["products"][0]["description"][:30] in ui.texts()
    ui.click("Add to cart")
    assert ui.tool_calls == ["add_to_cart"] and ui.toasts[-1] == "Added to your ShopSphere cart"
    ui.click("View cart & checkout")
    shown = ui.texts()
    assert ui.tool_calls[-1] == "view_cart" and "Your ShopSphere cart" in shown and "ShopSphere picks" not in shown
    assert found["products"][0]["name"] in shown
    for choice in ("Sign in & check out", "Create account & check out", "Check out as guest", "Back to products"):
        ui.button(choice)
    ui.click("+")                                                   # quantity changes update the cart screen
    assert ui.state["cart_count"] == 2 and ui.state["cart_lines"][0]["qty"] == 2
    ui.click("−")
    assert ui.state["cart_count"] == 1
    ui.click("Check out as guest")
    assert ui.tool_calls[-1] == "open_guest_checkout" and ui.sign_in_requests == 0
    assert "Your ShopSphere cart" not in ui.texts()
    order_id = _guest_pays_in_the_card(client, ui)
    assert ui.sign_in_requests == 0                                 # a guest is never asked to sign in
    # later, in a new chat: "track my order" -> the tracking card (Order ID + checkout email)
    data, structured = card_of(client, "track_order", order_id=order_id, email="PAT.CARD@guestmail.net")
    no_chat_messages_or_helper_clashes(structured)
    assert data["found"] and data["order"]["ship_to"] == {"city": "Austin", "state": "TX"}
    card = CardDriver(client, structured)
    assert "500 Congress" not in json.dumps(structured)             # city and state only, never the street
    assert card.state["t_badge"] == "Shipped" and "Austin, TX" in card.texts()   # moved on in the first card
    assert "Back" not in card.buttons() and "Delivered" in card.texts()
    for _ in range(2):
        card.click("Simulate next step (demo)")
    assert card.state["t_delivered"] and card.state["t_headline"].startswith("Delivered")
    assert "Simulate next step (demo)" not in card.buttons() and "Delivered to Austin, TX." in card.texts()
    wrong, structured = card_of(client, "track_order", order_id=order_id, email="someone@else.net")
    assert wrong == {"found": False, "message": wrong["message"]}
    lost = CardDriver(client, structured)
    assert "Order not found" in lost.texts() and "Shipment progress" not in lost.texts()


def test_cart_card_guest_button_opens_the_form_in_place(client):
    found, _ = card_of(client, "search_shopsphere", query="running shoes", size="10")
    cart = found["visitor_cart"]
    call(client, "add_to_cart", product_id="SSP049", color="White", visitor_cart=cart)
    _, structured = card_of(client, "show_cart", visitor_cart=cart)
    no_chat_messages_or_helper_clashes(structured)
    ui = CardDriver(client, structured)
    assert "Your ShopSphere cart" in ui.texts() and "Back to products" not in ui.buttons()
    ui.click("Check out as guest")
    assert ui.tool_calls == ["open_guest_checkout"] and "Apple AirPods Pro" in ui.texts()
    _guest_pays_in_the_card(client, ui)


def test_a_stale_customer_token_never_turns_guest_checkout_into_a_sign_in(client):
    """Claude keeps sending an old (expired) ShopSphere token: guest work still runs as a visitor,
    while customer tools with that token still ask for sign-in."""
    base = client.get("/.well-known/oauth-protected-resource/mcp").json()["resource"]
    expired = mcp_auth._issue("mcp_access", -60, sub="USR001", aud=base, cid="claude")
    found, structured = card_of(client, "search_shopsphere", expired, query="running shoes", size="10")
    ui = CardDriver(client, structured, expired)
    ui.click("Add to cart")
    ui.click("View cart & checkout")
    ui.click("Check out as guest")
    _guest_pays_in_the_card(client, ui)
    assert ui.sign_in_requests == 0
    for bad in (expired, "not-a-real-token"):
        for customer_tool in ("checkout", "open_checkout", "my_orders"):
            r, _ = rpc(client, "tools/call", {"name": customer_tool, "arguments": {}}, bad)
            assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]


def test_guest_card_uses_the_authorized_profile_and_a_sandbox_card_without_a_form(client):
    """Cart card → Check out as guest → "Yes, use it" → a sandbox card → Approve & pay: no form,
    no card entry, no sign-in; the order is placed once and tracked in the card."""
    found, _ = card_of(client, "search_shopsphere", query="running shoes", size="10")
    call(client, "add_to_cart", product_id="SSP001", size="10", color="Black", visitor_cart=found["visitor_cart"])
    _, cart_card = card_of(client, "show_cart", visitor_cart=found["visitor_cart"])
    ui = CardDriver(client, cart_card)
    ui.click("Check out as guest")
    shown = ui.texts()
    assert "I have your shipping information available" in shown and "Jordan Lee" in shown
    assert "San Francisco, CA 94105" in shown and "g_name" not in ui.inputs()
    assert not [b for b in ui.buttons() if b.startswith("Approve & pay")]
    ui.click("Yes, use it")
    assert ui.tool_calls[-1] == "card_use_guest_profile" and ui.state["profile_used"]
    assert "Ship to: Jordan Lee, San Francisco, CA 94105" in ui.texts()
    assert {"Visa •••• 4242 (sandbox)", "Mastercard •••• 4444 (sandbox)"} <= set(ui.buttons())
    ui.fill(delivery="express")
    ui.click("Visa •••• 4242 (sandbox)")
    assert ui.tool_calls[-1] == "card_review_guest_order" and ui.state["review_ready"]
    assert "Pay with: Visa •••• 4242 (sandbox)" in ui.texts() and "Delivery: Express" in ui.texts()
    ui.click(f"Approve & pay {ui.state['total_text']}")
    assert ui.tool_calls[-1] == "place_order" and ui.state["paid"] and ui.sign_in_requests == 0
    order_id = ui.state["order_ref"]
    shown = ui.texts()
    assert re.fullmatch(r"SS-\d{5}", order_id) and "✓ Your ShopSphere order has been placed" in shown
    assert "Confirmation sent to: j***@guest.shopsphere.demo" in shown
    with get_session() as db:
        (order,) = db.query(Order).filter(Order.display_id == order_id).all()
        assert order.payment_last4 == "4242" and order.guest_email == "jordan.lee@guest.shopsphere.demo"
    ui.click("Track my order")
    assert ui.state["t_order_id"] == order_id


def test_the_model_path_is_a_conversation_and_the_button_path_keeps_the_form(client):
    """Asked in the chat ("continue as a guest"), guest_checkout answers with data for the
    conversation (no form card); the cart card's "Check out as guest" button still draws the form."""
    found, _ = card_of(client, "search_shopsphere", query="running shoes", size="10")
    call(client, "add_to_cart", product_id="SSP049", color="White", visitor_cart=found["visitor_cart"])
    data, raw = card_of(client, "guest_checkout", visitor_cart=found["visitor_cart"])
    assert not (raw or {}).get("state") and data["guest_token"] and data["saved_profile"]["available"]
    _, cart_card = card_of(client, "show_cart", visitor_cart=found["visitor_cart"])
    ui = CardDriver(client, cart_card)
    ui.click("Check out as guest")
    assert ui.tool_calls[-1] == "open_guest_checkout" and ui.sign_in_requests == 0
    _guest_pays_in_the_card(client, ui)


# ── signed in ──────────────────────────────────────────────────────────────────

def test_visitor_signs_in_from_the_card_then_checks_out_and_pays_in_place(client):
    found, structured = card_of(client, "search_shopsphere", query="running shoes", size="10")
    ui = CardDriver(client, structured)
    ui.click("Add to cart")
    ui.click("View cart & checkout")
    ui.click("Sign in & check out")                                 # not signed in: Claude's sign-in window
    assert ui.sign_in_requests == 1 and "Sign in (or create your account)" in ui.toasts[-1]
    assert "Your ShopSphere cart" in ui.texts()                     # still on the cart, nothing lost
    token = sign_in(client, "chris@shopsphere.demo")
    _empty_cart(client, token)
    ui.token = token                                                # Claude now sends the new token
    ui.click("Sign in & check out")
    shown = ui.texts()
    assert ui.tool_calls[-1] == "open_checkout" and "Review your ShopSphere order" in shown
    assert found["products"][0]["name"] in shown                    # the visitor cart came along
    assert "Change your order" in shown and ui.state["c_payable"]
    _choose_and_pay(ui)


def test_signed_in_shopper_product_card_to_paid_order_in_place(client):
    token = sign_in(client, "kaajal@shopsphere.demo")
    _empty_cart(client, token)
    found, structured = card_of(client, "search_shopsphere", token, query="running shoes", size="10")
    assert "visitor_cart" not in found
    no_chat_messages_or_helper_clashes(structured)
    ui = CardDriver(client, structured, token)
    ui.click("Add to cart")
    ui.click("View cart & checkout")
    assert ui.state["signed_in"] and "Checkout" in ui.buttons() and "Check out as guest" not in ui.buttons()
    ui.click("Checkout")
    assert ui.tool_calls[-1] == "open_checkout" and "Review your ShopSphere order" in ui.texts()
    order_id = _choose_and_pay(ui)
    # "show my orders": each order with photo, status and progress; Track opens the full tracking
    data, structured = card_of(client, "my_orders", token)
    no_chat_messages_or_helper_clashes(structured)
    assert data["orders"][0]["order_id"] == order_id
    mine = CardDriver(client, structured, token)
    assert "Your ShopSphere orders" in mine.texts() and order_id in mine.texts()
    assert mine.state["o_orders"][0]["badge"] == "Shipped" and mine.state["o_orders"][0]["photo"]
    mine.click("Track")
    assert mine.tool_calls == ["order_status"] and mine.state["t_order_id"] == order_id
    assert "Shipment progress" in mine.texts() and "Your ShopSphere orders" not in mine.texts()
    mine.click("Back")
    assert "Your ShopSphere orders" in mine.texts()
    one, structured = card_of(client, "my_orders", token, order_id=order_id)
    assert one["found"] and CardDriver(client, structured, token).state["t_order_id"] == order_id


def _choose_and_pay(ui: CardDriver) -> str:
    work = [b for b in ui.buttons() if b.startswith("Ship to") and not b.startswith("✓")]
    if work:                                                        # another saved address: switch to it
        ui.click(work[0])
        assert any(b.startswith("✓ " + work[0]) for b in ui.buttons()) and ui.toasts[-1] == "Order updated"
    version = ui.state["c_version"]
    ui.click("Express delivery")
    assert "Express" in ui.state["c_delivery_label"] and ui.state["c_version"] > version
    visa = [b for b in ui.buttons() if b.startswith("Pay with") and "Visa" in b and not b.startswith("✓")]
    if visa:
        ui.click(visa[0])
    ui.click(f"Approve & pay {ui.state['c_total_text']}")
    assert ui.tool_calls[-1] != "approve_and_pay" and "Confirm with passkey" in ui.buttons()   # one more tap
    ui.click("Confirm with passkey")
    shown = ui.texts()
    assert ui.tool_calls[-1] == "approve_and_pay" and ui.state["c_paid"], ui.toasts
    order_id = ui.state["c_order_ref"]
    assert f"✓ Order {order_id} confirmed" in shown and "Change your order" not in shown
    ui.click("Track my order")
    check_tracking_screen(ui, order_id, back_to=f"✓ Order {order_id} confirmed")
    with get_session() as db:
        assert db.query(Order).filter(Order.display_id == order_id).count() == 1
    return order_id


# ── approvals ──────────────────────────────────────────────────────────────────

def test_claude_asks_only_before_checkout_and_before_paying(client):
    _, msg = rpc(client, "tools/list")
    flags = {t["name"]: (t.get("annotations") or {}) for t in msg["result"]["tools"]}
    ask = {name for name, a in flags.items() if not a.get("readOnlyHint")}
    assert ask == {"checkout", "open_checkout", "open_guest_checkout",
                   "approve_and_pay", "start_new_card_payment", "start_guest_payment", "place_order"}
    assert all(flags[n].get("destructiveHint") for n in ("approve_and_pay", "start_new_card_payment",
                                                          "start_guest_payment", "place_order"))
    for click in ("search_shopsphere", "add_to_cart", "view_cart", "show_cart", "update_cart_line",
                  "update_checkout_choices", "update_guest_checkout", "order_status", "track_order",
                  "my_orders", "advance_demo_shipment", "guest_checkout", "use_guest_profile", "review_order",
                  "decline_order"):
        assert flags[click]["readOnlyHint"] is True, click
