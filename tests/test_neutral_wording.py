"""
What Claude receives and what the live trace shows name each part by its role ("Visa •••• 4242",
"Card issuer", "Payment gateway / PSP") instead of demo labels ("(sandbox)", "Simulated card
issuer", "Mock gateway"). Ids are never touched, and Claude can still say it's a demo if asked.
"""
import json
import re

import pytest

from backend.db.schema import ProductVariant
from backend.db.session_utils import get_session
from backend.mcp.shopsphere_app import INSTRUCTIONS
from backend.observability import protocol_trace as pt
from backend.shop.wording import neutral, neutral_json_text
from tests.test_mcp_agentic_commerce import guest_buys_reply
from tests.test_mcp_shop_flow import client, rpc  # noqa: F401 (fixture)

DEMO_WORDS = re.compile(r"(?i)\b(simulat\w*|sandbox|mock|dummy|fictional|fake|test cards?)\b")


@pytest.fixture(autouse=True)
def _stock_back():
    with get_session() as db:
        before = {v.sku: v.stock for v in db.query(ProductVariant)}
    yield
    with get_session() as db:
        for v in db.query(ProductVariant):
            if v.sku in before and v.stock != before[v.sku]:
                v.stock = before[v.sku]
        db.commit()


@pytest.mark.parametrize("label, shown", [
    ("Simulated (in-process)", "In-process call"),
    ("Simulated card issuer", "Card issuer"),
    ("Mock gateway / PSP", "Payment gateway / PSP"),
    ("Visa •••• 4242 (sandbox), standard delivery", "Visa •••• 4242, standard delivery"),
    ("Demo agent profile (fictional, sandbox)", "Saved shopper profile"),
    ("Approval recorded (simulated passkey; no issuer contacted)", "Approval recorded"),
    ("The (simulated) card issuer asked the customer to verify", "The card issuer asked the customer to verify"),
    ("Simulated Secure Payment Provider", "Secure Payment Provider"),
    ("SIMULATED", "Hosted payment page"),
    ("level sandbox_chat, id sbx_a1b2c3d4e5", "level sandbox_chat, id sbx_a1b2c3d4e5"),   # ids never change
])
def test_labels_are_named_by_role(label, shown):
    assert neutral(label) == shown


def test_json_results_stay_valid_json():
    out = json.loads(neutral_json_text('{"pay_with": "Visa •••• 4242 (sandbox)", "level": "sandbox_chat"}'))
    assert out == {"pay_with": "Visa •••• 4242", "level": "sandbox_chat"}


def test_a_guest_order_shows_no_demo_labels_to_claude_or_on_the_trace(client):
    run = guest_buys_reply(client, card="4242")
    model_saw = json.dumps({k: v for k, v in run.items()}, ensure_ascii=False)
    assert not DEMO_WORDS.search(model_saw), DEMO_WORDS.search(model_saw).group(0)
    assert run["review"]["pay_with"] == "Visa •••• 4242" and run["placed"]["status"] == "authorized"
    rows = pt.events(run["placed"]["trace_url"].rsplit("/", 1)[1])
    shown = json.dumps([{k: v for k, v in e.items() if k != "layer"} for e in rows], ensure_ascii=False)
    assert rows and not DEMO_WORDS.search(shown), DEMO_WORDS.search(shown).group(0)
    page = client.get("/trace/" + run["placed"]["trace_url"].rsplit("/", 1)[1]).text
    assert "Simulated only" not in page and "ShopSphere demo environment" in page


def test_tool_descriptions_have_no_demo_labels_and_claude_answers_honestly_if_asked(client):
    _, msg = rpc(client, "tools/list")
    listed = json.dumps(msg["result"]["tools"])
    assert not DEMO_WORDS.search(listed), DEMO_WORDS.search(listed).group(0)
    assert "no real money moves" in INSTRUCTIONS                        # the honest answer, when asked
    assert not DEMO_WORDS.search(INSTRUCTIONS)
