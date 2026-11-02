"""
Talkshop's LLM calls. Two jobs only:

  decide()     — read a free-text message in context and choose an action
                 (search / select / choose option / checkout / answer …) with
                 its arguments, e.g. the structured search intent.
  recommend()  — one-line reasons for the top 3, written only from the
                 product facts ShopSphere returned, plus the size/colour the
                 shopper asked for, read the way a person would (typos too).

The model never produces prices, stock, totals or order numbers that get
shown as fact — those come from tool results. Every call has a
deterministic fallback so a model hiccup can't stall the conversation.
"""
import asyncio
import json
import logging
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from backend.agents.vibecheck import _content_text
from backend.guardrails.validators import extract_json_from_llm
from backend.talkshop.tools import LLM_TOOLS

log = logging.getLogger(__name__)

CATEGORIES = ["running_shoes", "sneakers", "boots", "shoes", "clothing", "accessories", "bags", "sunglasses",
              "watches", "electronics", "earbuds", "headphones", "speakers", "phones", "laptops"]


# A shopper is waiting: fail fast to the deterministic fallback rather than
# sit in the client's default retry backoff (seen at 200s+ on rate limits).
LLM_TIMEOUT = 10.0


def _llm(temperature: float = 0.1):
    from backend.config.llm import get_llm
    return get_llm(temperature=temperature, timeout=LLM_TIMEOUT, max_retries=1)


async def _ask(llm, messages):
    return await asyncio.wait_for(llm.ainvoke(messages), timeout=LLM_TIMEOUT + 2)


_DECIDE_SYSTEM = """You are Talkshop, the shopping assistant built into the ShopSphere store.
You read the shopper's message and choose ONE action for ShopSphere's systems to perform.
You never invent products, prices, stock, delivery dates or order numbers.

Actions you may choose (only from the allowed list you are given):
{tools}

Return ONLY a JSON object:
{{
  "action": "<one allowed action>",
  "args": {{
    "query": "<for search: the shopper's need in a few words, e.g. 'everyday running'>",
    "category": "<for search: one of {categories} or null>",
    "brand": "<brand named by the shopper or null>",
    "max_price": <number or null>,
    "gender": "<women | men | null>",
    "color": "<colour named or null>",
    "product_index": <for select_product: 0-based index into SHOWN PRODUCTS, or null>,
    "size": "<for choose_option: exactly one of the offered sizes; for search: the size the shopper named, or null>",
    "color_choice": "<for choose_option: exactly one of the offered colours, or null>",
    "delivery_method": "<for update_checkout: standard | express | null>",
    "quantity": <for update_checkout: number or null>
  }},
  "reply": "<for answer only: 1-3 friendly sentences using ONLY the facts provided>"
}}

Rules:
- A new product request ("I need…", "show me…", "do you have…") is "search", even mid-flow.
- ShopSphere sells ONLY clothing, shoes, accessories and electronics. A request for anything else (groceries,
  furniture, toys…) is "answer": say plainly ShopSphere doesn't sell it and offer what it does sell. Never search.
- Words after "no"/"not"/"don't want" are things the shopper does NOT want — never search for them.
- Do NOT ask for size or colour before searching — search with what you have.
- A question about a product shown ("is it good for flat feet?") is "answer", using only the facts given.
- If the shopper agrees to check out, choose "checkout"; if they decline, "keep_shopping".
- "Can I add more items", "continue shopping", "something else" WITHOUT a product named is "keep_shopping",
  never "search". With a product named ("add some socks too"), it is "search".
- Greetings and thanks are "answer".
- If CONTEXT has page_product, "this", "it" or "this one" means that product. Wanting to buy it or pick its
  size/colour is "select_product" (product_index null); a question about it is "answer".
- Never choose an action that is not in the allowed list."""


def _context_block(ctx: dict) -> str:
    return json.dumps(ctx, ensure_ascii=False, default=str)


async def decide(message: str, *, stage: str, allowed: list[str], context: dict, history: list[dict]) -> dict:
    """Returns {"action", "args", "reply"}; falls back to a plain search/answer on any failure."""
    tools = "\n".join(f"- {name}: {LLM_TOOLS[name]}" for name in allowed if name in LLM_TOOLS)
    recent = "\n".join(f"{h['role']}: {h['text']}" for h in history[-6:])
    prompt = (f"STAGE: {stage}\nALLOWED ACTIONS: {', '.join(allowed)}\n"
              f"CONTEXT (facts from ShopSphere): {_context_block(context)}\n"
              f"RECENT CONVERSATION:\n{recent or '(none)'}\n\nSHOPPER: {message}")
    try:
        resp = await _ask(_llm(), [
            SystemMessage(content=_DECIDE_SYSTEM.format(tools=tools, categories=", ".join(CATEGORIES))),
            HumanMessage(content=prompt),
        ])
        out = json.loads(extract_json_from_llm(_content_text(resp.content)))
        action = out.get("action")
        if action not in allowed:
            raise ValueError(f"action {action!r} not allowed in {stage}")
        args = out.get("args") or {}
        if args.get("category") not in CATEGORIES:
            args["category"] = None
        try:
            args["max_price"] = float(args["max_price"]) if args.get("max_price") not in (None, "") else None
        except (TypeError, ValueError):
            args["max_price"] = None
        return {"action": action, "args": args, "reply": (out.get("reply") or "").strip()}
    except Exception as exc:  # model/network/parse failure → safe deterministic default
        log.warning("talkshop.decide fallback: %s", exc)
        if "search" in allowed:
            return {"action": "search", "args": {"query": message}, "reply": ""}
        return {"action": "answer", "args": {},
                "reply": "Sorry, I didn't quite catch that — could you say it another way?"}


_RECOMMEND_SYSTEM = """You write short product recommendation copy for the ShopSphere store.
Use ONLY the facts given (name, brand, price, rating, tags, description). Never invent features,
discounts, stock or delivery promises. Do not repeat prices — the cards already show them.
Also note the size and colour the shopper asked for, if any, reading them as a shop assistant would:
typos ("tale" means Teal), shades ("sea green" may mean Teal), "a 10" means size 10. The colour must be
EXACTLY one of the colours listed for these products; if nothing listed fits, use null.
Finally, judge whether these products are the KIND of thing the shopper asked for. ShopSphere only sells
clothing, shoes, accessories (bags, watches, sunglasses, belts, wallets, scarves) and electronics (headphones,
earbuds, speakers, laptops, phones, smartwatches, fitness trackers). If the shopper asked for a kind of product
ShopSphere doesn't sell (e.g. tomatoes, a sofa, dog food, a garden hose) and none of these products is that kind of
thing, set "fits" to false and "asked_for" to the KIND of product they asked for, as a short noun phrase that
fits "ShopSphere doesn't sell ___" (e.g. "garden hoses", "cookware", "dog food"). Words after "no"/"not" are things
they do NOT want. If they asked for several things, "fits" is true when these products match ANY of them.
Price, colour, size or brand differences never make "fits" false, and vague requests ("a gift",
"something nice") fit.
Return ONLY JSON: {"intro": "<one friendly sentence>", "reasons": {"<product_id>": "<max 12 words why it fits>"},
"wants": {"color": "<a listed colour or null>", "size": "<size named by the shopper or null>"},
"fits": <true|false>, "asked_for": "<the kind of product, if fits is false, else null>"}"""


def fallback_reasons(products: list[dict]) -> dict:
    return {p["product_id"]: f"Rated {p['rating']}★ — {', '.join(p['tags'][:2])}." for p in products}


async def recommend(request: str, products: list[dict]) -> dict:
    facts = [{"product_id": p["product_id"], "name": p["name"], "brand": p["brand"], "rating": p["rating"],
              "review_count": p["review_count"], "tags": p["tags"], "description": p["description"],
              "colors": [c["name"] for c in p["colors"]], "sizes": p["sizes"],
              "department": p.get("department"), "category": p.get("category")}
             for p in products]
    default = {"intro": f"I found {len(products)} option{'s' if len(products) != 1 else ''} for you:",
               "reasons": fallback_reasons(products), "wants": {}, "fits": True}
    try:
        resp = await _ask(_llm(0.3), [
            SystemMessage(content=_RECOMMEND_SYSTEM),
            HumanMessage(content=f"Shopper asked: {request}\nProducts: {json.dumps(facts)}"),
        ])
        out = json.loads(extract_json_from_llm(_content_text(resp.content)))
        reasons = {pid: str(r)[:120] for pid, r in (out.get("reasons") or {}).items()
                   if pid in default["reasons"] and "$" not in str(r)}   # no invented prices
        colours = {c["name"].lower(): c["name"] for p in products for c in p["colors"]}
        wants = out.get("wants") if isinstance(out.get("wants"), dict) else {}
        color = colours.get(str(wants.get("color") or "").strip().lower())   # only colours that exist
        size = str(wants["size"]).strip()[:10] if wants.get("size") not in (None, "", "null") else None
        return {"intro": str(out.get("intro") or default["intro"])[:200],
                "reasons": {**default["reasons"], **reasons},
                "wants": {k: v for k, v in (("color", color), ("size", size)) if v},
                # only an explicit false hides results; anything unclear shows them
                "fits": out.get("fits") is not False,
                "asked_for": str(out.get("asked_for") or "").strip()[:40] or None}
    except Exception as exc:
        log.warning("talkshop.recommend fallback: %s", exc)
        return default


_IMAGE_SYSTEM = """You help a shopper find products like the one in their photo at the ShopSphere store.
Look at the photo and return ONLY JSON:
{"query": "<3-6 words describing the item, e.g. 'black leather crossbody bag'>",
 "category": "<one of """ + ", ".join(CATEGORIES) + """ or null>",
 "color": "<main colour, one word, or null>"}
If the photo shows no product you could shop for, return {"query": null}."""


async def describe_image(image_base64: str, note: str = "") -> Optional[dict]:
    """Turn a pasted photo into search arguments (query/category/colour)."""
    data_url = image_base64 if image_base64.startswith("data:") else f"data:image/jpeg;base64,{image_base64}"
    try:
        resp = await _ask(_llm(), [
            SystemMessage(content=_IMAGE_SYSTEM),
            HumanMessage(content=[{"type": "text", "text": note or "Find products like this."},
                                  {"type": "image_url", "image_url": data_url}]),
        ])
        out = json.loads(extract_json_from_llm(_content_text(resp.content)))
        if not out.get("query"):
            return None
        return {"query": str(out["query"])[:80],
                "category": out.get("category") if out.get("category") in CATEGORIES else None,
                "color": (str(out["color"]).strip().title() if out.get("color") else None)}
    except Exception as exc:
        log.warning("talkshop.describe_image failed: %s", exc)
        return None
