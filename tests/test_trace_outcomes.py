"""
The live trace never shows a finished step as still waiting: a "pending" row (purchase authorization
requested, the customer's approval page, a sign-in) is closed with what really happened once it
happens (approved and used, declined, replaced, expired), and its original request stays as it was.
"""
import time

import pytest

from backend.db.schema import ProductVariant, ProtocolEvent
from backend.db.session_utils import get_session
from backend.observability import protocol_trace as pt
from backend.shop import agent_profile
from tests.test_mcp_agentic_commerce import FakeWallet, guest_buys_reply, start_guest
from tests.test_mcp_conversational_checkout import mcp_visitor, tool
from tests.test_mcp_shop_flow import client  # noqa: F401 (fixture)


@pytest.fixture(autouse=True)
def _stock_back():
    """These tests buy real variants; put the stock back so other test files see what they expect."""
    with get_session() as db:
        before = {v.sku: v.stock for v in db.query(ProductVariant)}
    yield
    with get_session() as db:
        for v in db.query(ProductVariant):
            if v.sku in before and v.stock != before[v.sku]:
                v.stock = before[v.sku]
        db.commit()


def rows_for(co: dict, operation: str) -> list[dict]:
    return [e for e in pt.events(pt.trace_id_for(mcp_visitor({"co": co})))
            if e["operation"] == operation and e["correlation"] == co["checkout_id"]]


def test_a_chat_yes_closes_the_authorization_request_as_approved_and_used(client):
    run = guest_buys_reply(client, reply="Yes, proceed")
    assert run["placed"]["status"] == "authorized"
    [req] = rows_for(run["co"], pt.AUTH_REQUESTED)
    assert req["status"] == "success"
    assert req["response"].endswith("→ Approved by the shopper's clear yes in the chat; used once")
    assert req["response"].startswith("Single-use request (15 min)")       # what was requested is kept
    assert not [e for e in pt.events(run["placed"]["trace_url"].rsplit("/", 1)[1]) if e["status"] == "pending"]


def test_a_newer_review_replaces_the_old_request_and_a_no_declines_the_new_one(client):
    _, _, co = start_guest(client)
    tool(client, "use_guest_profile", guest_token=co["guest_token"])
    tool(client, "review_order", guest_token=co["guest_token"], payment_method_id="4242")
    tool(client, "review_order", guest_token=co["guest_token"], payment_method_id="4444")
    tool(client, "decline_order", guest_token=co["guest_token"], reason="Shopper said no")
    old, new = rows_for(co, pt.AUTH_REQUESTED)
    assert old["status"] == "info" and "Replaced by a newer request" in old["response"]
    assert new["status"] == "info" and new["response"].endswith("→ Declined by the shopper in the chat; nothing charged")


def test_the_customer_approval_page_closes_the_step_up_and_the_request(client, monkeypatch):
    monkeypatch.setenv("AGENT_PROFILE_PROVIDER", "external")
    agent_profile.register_external_client(FakeWallet())
    try:
        _, _, co = start_guest(client)
        tool(client, "use_guest_profile", guest_token=co["guest_token"])
        review = tool(client, "review_order", guest_token=co["guest_token"], payment_method_id="1111")
        step = tool(client, "place_order", guest_token=co["guest_token"], authorization=review["authorization"],
                    customer_reply="yes please")
        [waiting] = rows_for(co, pt.STEP_UP)
        assert waiting["status"] == "pending"                              # really waiting on the customer
        assert rows_for(co, pt.AUTH_REQUESTED)[0]["status"] == "pending"
        assert client.post("/" + step["approval_page"].split("/", 3)[3] + "/approve").status_code == 200
        [approved] = rows_for(co, pt.STEP_UP)
        assert approved["status"] == "success" and "approved on ShopSphere's approval page" in approved["response"]
        tool(client, "place_order", guest_token=co["guest_token"], authorization=review["authorization"],
             customer_reply="done, approved")
        [req] = rows_for(co, pt.AUTH_REQUESTED)
        assert req["status"] == "success" and "clear yes in the chat and the approval page; used once" in req["response"]
    finally:
        agent_profile.register_external_client(None)


def test_a_request_nobody_answered_shows_as_expired_after_15_minutes():
    tid = "tr_test_expiry_0000000000"
    with get_session() as db:
        db.add(ProtocolEvent(trace_id=tid, at=time.time() - 16 * 60, source=pt.MCP_SERVER, destination=pt.CLAUDE,
                             protocol=pt.MCP, operation=pt.AUTH_REQUESTED, request="r", response="Single-use request",
                             status="pending", correlation="CHK_TEST", layer="real"))
        db.add(ProtocolEvent(trace_id=tid, at=time.time() - 60, source=pt.MCP_SERVER, destination=pt.CLAUDE,
                             protocol=pt.MCP, operation=pt.AUTH_REQUESTED, request="r", response="Single-use request",
                             status="pending", correlation="CHK_TEST2", layer="real"))
        db.commit()
    try:
        old, fresh = pt.events(tid)
        assert old["status"] == "info" and old["response"].endswith("→ Expired unused after 15 minutes; nothing charged")
        assert fresh["status"] == "pending" and fresh["response"] == "Single-use request"   # still really waiting
    finally:
        pt.clear(tid)


def test_a_row_saved_as_pending_before_this_fix_shows_what_happened_later_in_its_trace():
    tid = "tr_test_legacy_00000000000"
    base = dict(trace_id=tid, source=pt.MCP_SERVER, destination=pt.CLAUDE, protocol=pt.MCP, request="r",
                correlation="CHK_OLD", layer="real")
    with get_session() as db:
        db.add(ProtocolEvent(at=time.time() - 3600, operation=pt.AUTH_REQUESTED, response="Single-use request",
                             status="pending", **base))
        db.add(ProtocolEvent(at=time.time() - 3598, operation="Customer purchase authorization",
                             response="consumed", status="success", **base))
        db.commit()
    try:
        req = pt.events(tid)[0]
        assert req["status"] == "success" and req["response"].endswith("→ Approved by the shopper; used once")
    finally:
        pt.clear(tid)
