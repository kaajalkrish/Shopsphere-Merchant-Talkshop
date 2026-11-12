"""
Interactive UI regression test for guest checkout inside Claude's MCP App card.

`CardDriver` plays the part of the Prefab renderer that runs inside Claude: it reads the card
JSON the server returned (view tree + state), shows only what the card's conditions allow,
fills inputs into the bound state, and on a click runs exactly the actions the button declares
(toolCall -> tools/call over the MCP HTTP endpoint, then setState / openLink / showToast /
sendMessage with `$result`), the way the renderer does. So the test fails if a button is wired
to the wrong tool, sends a chat message instead, loses state, or never reveals the next step.

Flow: cart card -> Check out as guest -> guest form -> save address -> Approve & pay ->
secure provider page -> I've finished paying -> order confirmation, all in the same card.
"""
import json
import re

from backend.db.schema import Order
from backend.db.session_utils import get_session
from backend.mcp import auth as mcp_auth
from tests.test_mcp_shop_flow import _on_secure_page, call, client, rpc  # noqa: F401 (fixture)

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
        self.messages: list[str] = []
        self.links: list[str] = []
        self.toasts: list[str] = []

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
        if whole:                                                   # "{{ x }}" keeps the value's type
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
            items = self.resolve("{{ " + node["key"] + " }}", scope) if "{{" not in node["key"] else \
                self.resolve(node["key"], scope)
            for i, item in enumerate(items or []):
                inner = dict(scope, _item=item)
                for name, expr in (node.get("let") or {}).items():
                    inner[name] = item if "$item" in expr else i
                self._walk(node.get("children"), inner, out)
            return
        out.append({"node": node, "scope": scope,
                    "text": self.resolve(node.get("content") or node.get("label") or "", scope)})
        self._walk(node.get("children"), scope, out)

    def texts(self) -> str:
        return "\n".join(str(v["text"]) for v in self.visible() if v["text"])

    def inputs(self) -> set[str]:
        return {v["node"]["name"] for v in self.visible() if v["node"].get("type") in ("Input", "Select")}

    def button(self, label_start: str) -> dict:
        found = [v for v in self.visible() if v["node"].get("type") == "Button" and str(v["text"]).startswith(label_start)]
        assert found, f"no visible button {label_start!r}; visible buttons: " + ", ".join(
            str(v["text"]) for v in self.visible() if v["node"].get("type") == "Button")
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
                assert r.status_code == 200, f"{a['tool']}: HTTP {r.status_code} (a sign-in challenge?) {r.text[:200]}"
                res = msg["result"]
                if res.get("isError"):
                    self._run(a.get("onError"), dict(scope, _error=res["content"][0]["text"]))
                    return False
                result = res.get("structuredContent")
                if result is None:
                    result = json.loads(res["content"][0]["text"])
                elif a.get("unwrapResult") and "result" in result:
                    result = result["result"]
                if not self._run(a.get("onSuccess"), dict(scope, _result=result)):
                    return False
            elif kind == "setState":
                self.state[a["key"]] = self.resolve(a["value"], scope)
            elif kind == "openLink":
                self.links.append(self.resolve(a.get("url") or a.get("href"), scope))
            elif kind == "showToast":
                self.toasts.append(str(self.resolve(a.get("description") or a.get("message"), scope)))
            elif kind == "sendMessage":
                self.messages.append(self.resolve(a.get("content"), scope))
            else:
                raise AssertionError(f"unexpected action {kind}")
        return True


def _visitor_cart(client, token=None) -> tuple[str, dict]:
    r, msg = rpc(client, "tools/call", {"name": "search_shopsphere", "arguments": {"query": "running shoes", "size": "10"}},
                 token)
    cart = json.loads(msg["result"]["content"][0]["text"])["visitor_cart"]
    for pid, size in (("SSP049", None), ("SSP001", "10")):
        colour = next(c["name"] for c in call(client, "get_shopsphere_product", product_id=pid, size=size)["all_colors"]
                      if c["in_stock"])
        r, msg = rpc(client, "tools/call", {"name": "add_to_cart", "arguments": {
            "product_id": pid, "size": size, "color": colour, "visitor_cart": cart}}, token)
        assert r.status_code == 200 and not msg["result"].get("isError")
    r, msg = rpc(client, "tools/call", {"name": "show_cart", "arguments": {"visitor_cart": cart}}, token)
    assert r.status_code == 200
    return cart, msg["result"]["structuredContent"]


def _guest_flow_in_the_card(client, token=None) -> CardDriver:
    _, card_json = _visitor_cart(client, token)
    ui = CardDriver(client, card_json, token)
    shown = ui.texts()
    assert "Your ShopSphere cart" in shown and "Guest checkout at ShopSphere" not in shown
    for choice in ("Sign in & check out", "Create account & check out", "Check out as guest"):
        ui.button(choice)

    # 1. Check out as guest: a direct tool call, no chat message, no sign-in; the form opens in place
    ui.click("Check out as guest")
    assert ui.tool_calls == ["open_guest_checkout"] and not ui.messages
    shown = ui.texts()
    assert "Guest checkout at ShopSphere" in shown and "Your ShopSphere cart" not in shown
    assert "Apple AirPods Pro" in shown and "Add your name, email and shipping address." in shown
    assert {"g_name", "g_email", "g_line1", "g_line2", "g_city", "g_state", "g_zip", "delivery"} <= ui.inputs()
    assert not [v for v in ui.visible() if str(v["text"]).startswith("Approve & pay")]   # not payable yet
    assert ui.state["guest_token"] and ui.state["lines"]

    # 2. a bad email is refused on the card; then the real details are saved
    ui.fill(g_name="Pat Guest", g_email="not-an-email", g_line1="500 Congress Ave", g_city="Austin",
            g_state="TX", g_zip="78701")
    ui.click("Save details & update total")
    assert any("valid email" in t for t in ui.toasts) and not ui.state["has_details"]
    ui.fill(g_email="pat.card@guestmail.net", delivery="express")
    ui.click("Save details & update total")
    shown = ui.texts()
    assert "Ship to: Pat Guest, Austin, TX 78701" in shown and "Confirmation email to: p***@guestmail.net" in shown
    assert "Delivery: Express" in shown and ui.state["shipping_text"] == "$9.99" and ui.state["payable"]

    # 3. Approve & pay the exact total -> the secure provider page opens
    total = ui.state["total_text"]
    ui.click(f"Approve & pay {total}")
    assert ui.tool_calls[-1] == "start_guest_payment" and len(ui.links) == 1 and "/psp/hosted/" in ui.links[0]
    assert "Finish on the secure payment page" in ui.texts()
    ui.click("I've finished paying")                                # too early: nothing happens
    assert ui.toasts[-1] and "isn't finished" in ui.toasts[-1] and not ui.state["paid"]

    # 4. the shopper types the card on the provider's own page (never in the card or the chat)
    assert "Payment verified" in _on_secure_page(client, ui.links[0], save=True)

    # 5. back on the card: I've finished paying -> the order confirmation, in the same card
    ui.click("I've finished paying")
    shown = ui.texts()
    assert ui.state["paid"] and "✓ Your ShopSphere order has been placed" in shown
    order_id = ui.state["order_ref"]
    assert re.fullmatch(r"SS-\d{5}", order_id) and f"Order ID: {order_id}" in shown
    assert "Confirmation sent to: p***@guestmail.net" in shown and f"Estimated delivery: {ui.state['arrives']}" in shown
    assert "Save details" not in shown and not [v for v in ui.visible() if str(v["text"]).startswith("Approve")]
    assert ui.tool_calls == ["open_guest_checkout", "update_guest_checkout", "update_guest_checkout",
                             "start_guest_payment", "finish_guest_payment", "finish_guest_payment"]
    assert not ui.messages                                          # the whole checkout ran without the model
    with get_session() as db:
        orders = db.query(Order).filter(Order.display_id == order_id).all()
        assert len(orders) == 1 and orders[0].guest_email == "pat.card@guestmail.net"
    tracked = call(client, "track_order", order_id=order_id, email="pat.card@guestmail.net")
    assert tracked["found"]
    return ui


def test_guest_button_opens_the_form_in_the_card_and_completes_the_order(client):
    _guest_flow_in_the_card(client)


def test_a_stale_customer_token_never_turns_guest_checkout_into_a_sign_in(client):
    """Claude keeps sending an old (expired) ShopSphere token: guest operations still work as a
    visitor, while a customer tool with that token still asks for sign-in."""
    base = client.get("/.well-known/oauth-protected-resource/mcp").json()["resource"]
    expired = mcp_auth._issue("mcp_access", -60, sub="USR001", aud=base, cid="claude")
    _guest_flow_in_the_card(client, expired)
    for bad in (expired, "not-a-real-token"):
        r, _ = rpc(client, "tools/call", {"name": "checkout", "arguments": {}}, bad)
        assert r.status_code == 401 and "resource_metadata" in r.headers["www-authenticate"]
        r, _ = rpc(client, "tools/call", {"name": "my_orders", "arguments": {}}, bad)
        assert r.status_code == 401


def test_the_model_path_shows_the_same_guest_form(client):
    """Asked in the chat ("check out as a guest"), guest_checkout draws the same state-driven form."""
    cart, _ = _visitor_cart(client)
    r, msg = rpc(client, "tools/call", {"name": "guest_checkout", "arguments": {"visitor_cart": cart}})
    assert r.status_code == 200
    ui = CardDriver(client, msg["result"]["structuredContent"])
    assert "Guest checkout at ShopSphere" in ui.texts() and "g_email" in ui.inputs()
    ui.fill(g_name="Lee Guest", g_email="lee@guestmail.net", g_line1="1 Main St", g_city="Austin", g_state="TX",
            g_zip="78701")
    ui.click("Save details & update total")
    ui.click(f"Approve & pay {ui.state['total_text']}")
    assert ui.links and ui.tool_calls == ["update_guest_checkout", "start_guest_payment"]
