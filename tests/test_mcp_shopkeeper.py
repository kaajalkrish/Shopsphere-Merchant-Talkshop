"""
The shopkeeper conversation in Claude: before searching, get_shopping_questions asks only what
would narrow ShopSphere's in-stock products (who for, what for, size, colour, budget), offers only
options the shop really has, and says when to stop asking. Plus the small kids' section.
"""
from backend.talkshop import parse
from tests.test_mcp_conversational_checkout import tool
from tests.test_mcp_shop_flow import client  # noqa: F401 (fixture)


def ids(out: dict) -> list[str]:
    return [q["id"] for q in out["questions"]]


def test_a_broad_request_gets_questions_with_real_options_only(client):
    out = tool(client, "get_shopping_questions", request="I'm looking for running shoes")
    assert not out["ready_to_search"] and out["understood"] == {"category": "running_shoes"}
    assert ids(out)[:2] == ["for_whom", "use"]                         # who first, then what for
    who = out["questions"][0]["options"]
    assert set(who) == {"Women's", "Men's", "Unisex", "Kids'"}
    assert set(out["questions"][1]["options"]) == {"Road / everyday running", "Trail running"}
    colours = next(q for q in out["questions"] if q["id"] == "color")["options"]
    assert colours[-1] == "Any colour" and len(colours) <= 7
    budget = next(q for q in out["questions"] if q["id"] == "budget")
    lo, hi = budget["price_range"]
    assert lo == 54.99 and hi == 189.99 and budget["options"][-1] == "No budget in mind"
    assert "FIRST one or two questions" in out["next_step"]


def test_answers_narrow_it_down_and_known_things_are_never_asked_again(client):
    out = tool(client, "get_shopping_questions", request="running shoes for my wife, road running, under $150")
    assert out["understood"]["for_whom"] == "women" and out["understood"]["max_price"] == 150
    assert "for_whom" not in ids(out) and "use" not in ids(out) and "budget" not in ids(out)
    sizes = next(q for q in out["questions"] if q["id"] == "size")["options"]
    assert "13" not in sizes and "11C" not in sizes                     # women's + unisex sizes, no kids'
    done = tool(client, "get_shopping_questions", request="running shoes for my wife, road running, under $150",
                size="8", color="White")
    assert done["ready_to_search"] and done["questions"] == [] and 1 <= done["matches_now"] <= 3
    found = tool(client, "search_shopsphere", **{k: v for k, v in done["search_args"].items() if v is not None})
    assert found["found"] and all(p["price"] <= 150 for p in found["products"])
    assert "like a shopkeeper" in found["next_step"]


def test_for_my_son_asks_child_or_adult_then_uses_the_kids_section(client):
    out = tool(client, "get_shopping_questions", request="running shoes for my son")
    assert out["questions"][0] == {"id": "for_whom", "ask": "Is that for a child (kids' sizes) or an adult?",
                                   "options": ["A child (kids' sizes)", "An adult"]}
    child = tool(client, "get_shopping_questions", request="running shoes for my son", for_whom="A child (kids' sizes)")
    assert child["understood"]["for_whom"] == "kids" and child["ready_to_search"]
    adult = tool(client, "get_shopping_questions", request="running shoes for my son", for_whom="An adult")
    assert adult["understood"]["for_whom"] == "men" and "for_whom" not in ids(adult)
    kids = tool(client, "search_shopsphere", **{k: v for k, v in child["search_args"].items() if v is not None})
    assert kids["found"] and {p["name"] for p in kids["products"]} <= {"Kinetic Junior Runner",
                                                                       "Northpace Kids Trail Scout"}
    assert all(set(p["available_sizes"]) <= {"11C", "12C", "13C", "1Y", "2Y", "3Y", "4Y", "5Y", "6Y"}
               for p in kids["products"])


def test_kids_products_stay_out_of_adult_searches(client):
    adult = tool(client, "search_shopsphere", query="running shoes", limit=5)
    assert adult["found"] and not any("Kid" in p["name"] or "Junior" in p["name"] for p in adult["products"])
    kids = tool(client, "search_shopsphere", query="kids sneakers")
    assert [p["name"] for p in kids["products"]] == ["Sphere Kids Light-Up Sneaker"]
    assert parse.gender_of("shoes for my 7 year old") == "kids" and parse.gender_of("toddler hoodie") == "kids"
    assert parse.gender_of("shoes for my husband") == "men"            # unchanged for adults


def test_specific_requests_skip_the_questions_and_missing_kinds_are_said_plainly(client):
    out = tool(client, "get_shopping_questions", request="Nike running shoes men's size 10 black under $150")
    assert out["ready_to_search"] and out["questions"] == []
    none = tool(client, "get_shopping_questions", request="a laptop for my kids")
    assert not none["ready_to_search"] and none["matches_now"] == 0
    assert "Kinetic Junior Runner" in none["notes"][0] and "Nothing matches" in none["next_step"]
    vague = tool(client, "get_shopping_questions", request="I need a gift")
    assert vague["questions"][0]["id"] == "what" and len(vague["questions"][0]["options"]) == 4


def test_the_questions_tool_is_read_only_and_listed(client):
    from tests.test_mcp_shop_flow import rpc
    _, msg = rpc(client, "tools/list")
    tools = {t["name"]: t for t in msg["result"]["tools"]}
    assert tools["get_shopping_questions"]["annotations"]["readOnlyHint"] is True
