"""
ShopSphere MCP server for ChatGPT / Claude (backend/mcp/shopsphere_app.py), step 1:
read-only product discovery with the same catalog logic as the Talkshop panel.
"""
import asyncio
import json

from fastapi.testclient import TestClient
from fastmcp import Client

from backend.main import app
from backend.mcp.shopsphere_app import mcp


def run(coro):
    """A private event loop: asyncio.run() would clear the thread's current loop,
    which older tests in the suite (asyncio.get_event_loop()) rely on."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def call(tool: str, **args) -> dict:
    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool(tool, args)
            return r.structured_content or json.loads(r.content[0].text)
    return run(go())


def tools() -> list:
    async def go():
        async with Client(mcp) as c:
            return await c.list_tools()
    return run(go())


def test_only_read_only_discovery_tools_are_exposed():
    listed = tools()
    assert sorted(t.name for t in listed) == ["get_shopsphere_product", "search_shopsphere", "show_shopsphere_cards"]
    assert all(t.annotations and t.annotations.read_only_hint for t in listed)
    assert "ShopSphere" in (mcp.instructions or "") and "web search" in mcp.instructions


def test_size_10_shoes_are_in_stock_in_size_10():
    res = call("search_shopsphere", query="I'm looking for shoes in size 10")
    assert res["found"] and res["store"] == "ShopSphere" and res["size"] == "10"
    assert 1 <= len(res["products"]) <= 3
    for p in res["products"]:
        assert "10" in p["available_sizes"] and p["available_colors"]
        assert p["url"].endswith(f"/p/{p['product_id']}") and p["image_url"].startswith("http")


def test_a_size_that_is_sold_out_is_left_out():
    # Runner Pro X has no size 11 in stock in the seeded catalog
    ids = [p["product_id"] for p in call("search_shopsphere", query="running shoes", size="11")["products"]]
    assert ids and "SSP001" not in ids


def test_brand_shopsphere_does_not_carry_says_so_and_shows_similar():
    res = call("search_shopsphere", query="Gucci shoes")
    assert res["found"] and any("doesn't carry Gucci" in n for n in res["notes"])
    assert all(p["brand"] != "Gucci" for p in res["products"])


def test_not_sold_and_unmatched_requests_allow_web_search():
    for q in ("a sofa for my living room", "tomatoes", "golf clubs"):
        res = call("search_shopsphere", query=q)
        assert res["found"] is False and res["products"] == [], q
        assert "web" in res["next_step"] and "not ShopSphere" in res["next_step"]


def test_budget_and_colour_from_the_request_are_used():
    buds = call("search_shopsphere", query="wireless earbuds under $100")["products"]
    assert buds and all(p["price"] <= 100 for p in buds)
    black = call("search_shopsphere", query="black sneakers", size="9")["products"]
    assert black and all("Black" in p["available_colors"] for p in black)


def test_product_details_with_size_availability():
    p = call("get_shopsphere_product", product_id="SSP001", size="10")
    assert p["found"] and p["name"] == "Runner Pro X" and p["size_requested"] == "10"
    assert {"size": "11", "available": False} in p["sizes"]
    assert call("get_shopsphere_product", product_id="NOPE")["found"] is False


def test_product_cards_show_photos_prices_and_buttons():
    async def go():
        async with Client(mcp) as c:
            listed = {t.name: t for t in await c.list_tools()}
            ui = (listed["show_shopsphere_cards"].meta or {}).get("ui", {})
            assert ui.get("resourceUri", "").startswith("ui://")          # rendered as an MCP App card
            return await c.call_tool("show_shopsphere_cards", {"product_ids": ["SSP001", "SSP010", "NOPE"], "size": "10"})
    r = run(go())
    page = json.dumps(r.structured_content)
    assert page.count("data:image/") == 2                                   # photos embedded, unknown id skipped
    assert "Runner Pro X" in page and "$129.00" in page and "View on ShopSphere" in page and "Choose this" in page
    assert [p["product_id"] for p in r.structured_content["state"]["products"]] == ["SSP001", "SSP010"]


def test_mcp_endpoint_is_served_over_http_next_to_the_api():
    with TestClient(app) as c:
        assert c.get("/").json()["status"] == "ok"                   # existing routes unaffected
        assert c.get("/api/products").status_code == 401
        r = c.post("/mcp", headers={"Accept": "application/json, text/event-stream"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "pytest", "version": "1"}}})
        assert r.status_code == 200, r.text
        assert "ShopSphere" in r.text
