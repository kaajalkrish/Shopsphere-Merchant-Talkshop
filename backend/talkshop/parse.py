"""
Deterministic reading of short, predictable replies — "size 8", "black",
"the first one", "yes", "keep shopping". These are matched exactly against
the options ShopSphere actually offers, so the common path is fast and
never depends on the LLM guessing. Anything else goes to the LLM.
"""
import re
from typing import Optional

_NUM_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
              "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13"}
_SIZE_WORDS = {"extra small": "XS", "x-small": "XS", "small": "S", "medium": "M", "large": "L",
               "extra large": "XL", "x-large": "XL", "xx-large": "XXL", "double xl": "XXL"}
_COLOR_SYNONYMS = {"gray": "grey", "navy blue": "navy", "dark grey": "graphite", "rose gold": "gold"}
# Ordinal words first: in "the second one", "second" must win over "one".
_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
             "number one": 0, "number two": 1, "number three": 2}

YES = {"yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "please", "yes please", "proceed", "checkout",
       "check out", "yes checkout", "yes, checkout", "let's do it", "lets do it", "sounds good", "go for it",
       "proceed to checkout", "yes proceed", "do it", "sure thing"}
NO = {"no", "n", "nope", "not now", "keep shopping", "later", "maybe later", "no thanks", "no thank you",
      "not yet", "continue shopping", "nah"}
# Phrases that sound like payment consent. Typed text is NEVER consent — in the
# review stage these just point the shopper at the GO AHEAD button.
PAY_PHRASES = ("go ahead", "pay", "place order", "place the order", "buy now", "confirm", "complete",
               "purchase", "submit order", "checkout now")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower().rstrip(".!?"))


def is_yes(text: str) -> bool:
    return norm(text) in YES


def is_no(text: str) -> bool:
    return norm(text) in NO


# "Can I add more items / keep shopping?" without naming a product: the shopper wants
# to carry on shopping, not a search for the words "more items".
MORE_SHOPPING = ("add more", "more items", "more things", "more stuff", "more products", "continue shopping",
                 "keep shopping", "shop more", "shop some more", "shopping more", "keep browsing",
                 "continue browsing", "browse more", "something else", "add something", "add another item",
                 "add other items", "add a few", "few more items", "not done yet", "look around")


def wants_more_shopping(text: str) -> bool:
    t = norm(text)
    return any(p in t for p in MORE_SHOPPING)


_REMOVE = re.compile(r"\b(remove|delete|take (?:out|off)|drop|don'?t want|do not want|without|only (?:the|buy|want|get)|"
                     r"just (?:the|buy|want|get))\b")
_ONLY = re.compile(r"\b(only|just)\b")
_NAME_STOP = {"the", "and", "pro", "gen", "2nd", "with", "for", "new", "size"}


def wants_removal(text: str) -> bool:
    """"remove the shoes", "I don't want the AirPods", "only the AirPods" at the review card."""
    return bool(_REMOVE.search(norm(text)))


def keeps_only(text: str) -> bool:
    """"only the AirPods" / "just the AirPods": keep the named item, remove the rest."""
    return bool(_ONLY.search(norm(text)))


_SHOE_KINDS = {"shoes", "running_shoes", "sneakers", "boots"}


def lines_named(text: str, lines: list[dict]) -> list[dict]:
    """The order lines the shopper named: by a word of the product name ("runner", "airpods"),
    or by its kind ("the shoes", "the earbuds") when the line carries its department,
    category and subcategory."""
    t = norm(text)
    words = set(re.findall(r"[a-z0-9]+", t))
    kind = next((cat for pattern, cat in _CATEGORY_WORDS if _wanted(pattern, t)), None)
    hits = []
    for ln in lines:
        name_words = {w for w in re.findall(r"[a-z0-9]+", (ln.get("name") or "").lower())
                      if len(w) >= 4 and w not in _NAME_STOP}
        kinds = {ln.get("department"), ln.get("category"), ln.get("subcategory")} - {None}
        by_kind = bool(kind) and (kind in kinds or (kind in _SHOE_KINDS and "shoes" in kinds))
        if name_words & words or by_kind:
            hits.append(ln)
    return hits


def sounds_like_payment(text: str) -> bool:
    t = norm(text)
    return any(p in t for p in PAY_PHRASES) or t in YES


def pick_shown(text: str, names: list[str]) -> Optional[int]:
    """'the first one' / '#2' / 'the last' / a product's name → index into the shown list."""
    t = norm(text)
    if not names:
        return None
    for i, name in enumerate(names):                      # full name wins
        if norm(name) in t:
            return i
    if re.search(r"\blast\b", t):
        return len(names) - 1
    m = re.search(r"(?:#|number |no\.? ?|option )?\b([1-9])\b", t)
    if m and len(t) <= 20 and 1 <= int(m.group(1)) <= len(names):
        return int(m.group(1)) - 1
    for word, idx in _ORDINALS.items():
        if re.search(rf"\b{re.escape(word)}\b", t) and idx < len(names) and len(t) <= 30:
            return idx
    # A distinctive word from one name only ("the pegasus", "flexrun")
    words = {w for w in re.findall(r"[a-z0-9]+", t) if len(w) > 3}
    hits = [i for i, n in enumerate(names) if words & {w for w in re.findall(r"[a-z0-9]+", n.lower()) if len(w) > 3}]
    return hits[0] if len(hits) == 1 else None


def pick_size(text: str, sizes: list[str]) -> Optional[str]:
    """Match a reply to one of the offered sizes/options (8, M, 256GB, 41mm …)."""
    if not sizes:
        return None
    t = norm(text)
    for word, num in _NUM_WORDS.items():
        t = re.sub(rf"\b{word}\b", num, t)
    for phrase, code in sorted(_SIZE_WORDS.items(), key=lambda kv: -len(kv[0])):
        t = re.sub(rf"\b{phrase}\b", code.lower(), t)
    compact = t.replace(" ", "")
    by_norm = {s.lower(): s for s in sizes}
    for key in sorted(by_norm, key=len, reverse=True):     # "256gb" before "25"
        if re.search(rf"(?<![a-z0-9.]){re.escape(key)}(?![a-z0-9])", t) or compact == key:
            return by_norm[key]
    m = re.search(r"\b(\d{1,2})(?:\.0)?\b", t)
    if m and m.group(1) in sizes:
        return m.group(1)
    return None


_ASKED_SIZE = re.compile(r"\bsize\s*(?:of\s*|is\s*|:\s*)?(\d{1,4}(?:\.5)?(?:gb|tb|mm)?|x{0,3}[sl]|m)\b"
                         r"|\b(\d{1,2}(?:\.5)?)\s*size\b|\b(?:us|uk|eu)\s*(\d{1,2}(?:\.5)?)\b")


def asked_size(text: str) -> Optional[str]:
    """A size named inside a request ("teal nike shoes of size 10", "a medium tee
    in size m", "uk 9") — checked later against what the chosen product offers."""
    t = norm(text)
    for word, num in _NUM_WORDS.items():
        t = re.sub(rf"\bsize (?:of )?{word}\b", f"size {num}", t)
    for phrase, code in sorted(_SIZE_WORDS.items(), key=lambda kv: -len(kv[0])):
        t = re.sub(rf"\bsize (?:of )?{phrase}\b", f"size {code.lower()}", t)
    m = _ASKED_SIZE.search(t)
    return next((g for g in m.groups() if g), None) if m else None


def pick_color(text: str, colors: list[str]) -> Optional[str]:
    t = norm(text)
    for alias, real in _COLOR_SYNONYMS.items():
        t = re.sub(rf"\b{alias}\b", real, t)
    for c in sorted(colors, key=len, reverse=True):        # "light blue" before "blue"
        if re.search(rf"\b{re.escape(c.lower())}\b", t):
            return c
    # A small typo in a one-word colour ("tale" → Teal, "blak" → Black)
    words = [w for w in re.findall(r"[a-z]+", t) if len(w) >= 4 and w not in _NOT_COLOURS]
    close = [c for c in colors if " " not in c and len(c) >= 4 and any(_near(w, c.lower()) for w in words)]
    return close[0] if len(close) == 1 else None


# Everyday words a typo away from a colour name ("while" ~ white, "goal" ~ gold).
_NOT_COLOURS = {"while", "whine", "write", "great", "greet", "goal", "bold", "cold", "told", "sold", "hold",
                "fold", "mold", "glue", "true", "grew", "tall", "teen", "team", "tear", "real", "deal", "meal",
                "seal", "steal", "liver", "wave", "save", "have", "alive", "love", "crown", "grown", "drown",
                "frown", "blank", "block", "moral", "oral", "dream", "nose", "hose", "pose", "lose", "hint",
                "tint", "mind", "pine", "rest", "tile"}


def _near(word: str, colour: str) -> bool:
    """Same first letter and one edit away, or the same letters reordered."""
    if word[0] != colour[0] or abs(len(word) - len(colour)) > 1:
        return False
    if len(word) == len(colour) and sorted(word) == sorted(colour):
        return True
    prev = list(range(len(colour) + 1))                     # Levenshtein distance ≤ 1
    for i, a in enumerate(word, 1):
        cur = [i]
        for j, b in enumerate(colour, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a != b)))
        prev = cur
    return prev[-1] <= 1


def gender_of(text: str) -> Optional[str]:
    t = norm(text)
    if re.search(r"\b(women'?s?|woman|ladies|lady|her|girls?|female|sister|wife|mom|mum|mother|"
                 r"girlfriend|daughter|aunt|grandma)\b", t):
        return "women"
    if re.search(r"\b(men'?s?|man|guys?|him|boys?|male|gents?|brother|husband|dad|father|"
                 r"boyfriend|son|uncle|grandpa)\b", t):
        return "men"
    return None


# ── Payment and address data typed into the chat ─────────────────────────────
# Card details belong in the Secure Payment form only. If a shopper types them
# anyway, they are masked before the message is shown, stored or read by the LLM.

_CARD_RUN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_CVC = re.compile(r"\b(cvv2?|cvc2?|cid|security code)\s*(?:is|:|#)?\s*\d{3,4}\b", re.I)
_EXPIRY = re.compile(r"\b(exp(?:iry|ires|iration)?(?: date)?)\s*(?:is|:)?\s*\d{1,2}\s*/\s*\d{2,4}\b", re.I)
_ADDRESS = re.compile(r"\b\d{1,6}\s+[a-z0-9 .'-]{2,40}\b(st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|"
                      r"way|ct|court|pl|place|pkwy|parkway|hwy|highway|cir|circle|ter|terrace)\b", re.I)
_ZIP = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_MASKED = re.compile(r"•••• \d{4}|\b(?:cvv2?|cvc2?|cid|security code) •••|••/••", re.I)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def redact_payment_data(text: str) -> tuple[str, bool]:
    """Mask card numbers (Luhn-valid runs of 13–19 digits), CVCs and expiry
    dates. Returns (masked text, whether anything was masked)."""
    found = False

    def card(m: re.Match) -> str:
        nonlocal found
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            found = True
            return f"•••• {digits[-4:]}"
        return m.group(0)

    out = _CARD_RUN.sub(card, text)
    out, n_cvc = _CVC.subn(lambda m: f"{m.group(1)} •••", out)
    out, n_exp = _EXPIRY.subn(lambda m: f"{m.group(1)} ••/••", out)
    already = bool(_MASKED.search(out))      # the browser masks first (frontend/src/talkshop/redact.ts)
    return out, found or bool(n_cvc or n_exp) or already


# ── Orders already placed (Phase 11) ─────────────────────────────────────────

_ORDER_ID = re.compile(r"\bSS[\s-]?(\d{5})\b", re.I)
_TRACKING = re.compile(r"\b(track(?:ing)?|where(?:'s| is) my (?:order|package|parcel|delivery|stuff)|order status|"
                       r"status of my order|my order|has my order|when will my order|delivery status)\b", re.I)
# About an order already placed, even when it names a product ("I placed an order of nike shoes")
_PAST_ORDER = re.compile(r"\b(placed (?:an |my |the |a )?order|i (?:ordered|bought|purchased)|my purchase|"
                         r"track(?:ing)?|where(?:'s| is) my|order status|delivery status|has my order|"
                         r"when will my order|did my order)\b", re.I)
_ORDER_STOPWORDS = {
    "the", "and", "order", "orders", "ordered", "placed", "place", "track", "tracking", "where", "when", "what",
    "status", "delivery", "show", "can", "you", "please", "hey", "with", "this", "that", "for", "have", "has",
    "did", "was", "were", "from", "about", "details", "detail", "package", "parcel", "arrive", "arriving", "bought",
    "purchased", "purchase", "item", "items", "some", "pair", "get", "got", "yet", "still", "its", "whats", "here",
    "there", "of", "my", "me", "is", "it", "an", "a", "i", "im", "are", "will", "be", "know", "want", "need", "check",
    "last", "week", "weeks", "any", "update", "updates", "yesterday", "today", "days", "ago", "recently", "recent",
}
_EMAIL_IN_TEXT = re.compile(r"[^@\s,;:<>()]+@[^@\s,;:<>()]+\.[a-zA-Z]{2,}")


def order_ref(text: str) -> Optional[str]:
    """'order ss 12345' / 'SS-12345' → 'SS-12345'."""
    m = _ORDER_ID.search(text or "")
    return f"SS-{m.group(1)}" if m else None


_POINTS = re.compile(r"\b(loyalty|reward points?|rewards|my points|points balance|how many points|"
                     r"points (?:do i have|have i got)|point balance)\b", re.I)


def asks_about_points(text: str) -> bool:
    """'how many points do I have?', 'my loyalty points'…"""
    return bool(_POINTS.search(text or ""))


def about_past_order(text: str) -> bool:
    """Clearly about an order already placed, even if it names a product."""
    return bool(_PAST_ORDER.search(text or ""))


def order_keywords(text: str) -> list[str]:
    """Words that could identify which order ('nike shoes' → ['nike', 'shoes'])."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [w for w in words if len(w) >= 3 and w not in _ORDER_STOPWORDS and not re.fullmatch(r"ss\d*|\d+", w)]


def wants_tracking(text: str) -> bool:
    """'where is my order', 'track my package', 'order status'…"""
    return bool(_TRACKING.search(text or ""))


def email_in(text: str) -> Optional[str]:
    m = _EMAIL_IN_TEXT.search(text or "")
    return m.group(0).strip(".").lower() if m else None


def mask_emails(text: str) -> str:
    return _EMAIL_IN_TEXT.sub("[email]", text or "")


def looks_like_address(text: str) -> bool:
    """"12 Oak Street, Austin TX 78704" — a street line, or a street number plus a ZIP."""
    return bool(_ADDRESS.search(text)) or (bool(_ZIP.search(text)) and bool(re.match(r"\s*\d{1,6}\s+[a-z]", text, re.I)))


# ── Clear shopping requests (skip the LLM round-trip) ────────────────────────

_CATEGORY_WORDS = [  # longest/most specific first
    (r"running shoes?|running sneakers?|jogging shoes?|runners", "running_shoes"),
    (r"hiking boots?|chelsea boots?|boots?", "boots"),
    (r"sneakers?|trainers|high[- ]tops?|canvas shoes?", "sneakers"),
    (r"sandals?|flip[- ]flops?|slides|heels|pumps|dress shoes?|oxfords?|derbys?|loafers?|shoes?", "shoes"),
    (r"smart ?watch(?:es)?|apple watch|fitness (?:tracker|band)s?|watch(?:es)?", "watches"),
    # audio before phones: "earphone" / "ear phones" are earbuds, never phones
    (r"ear ?buds?|ear ?phones?|in-?ears?|airpods?|true wireless", "earbuds"),
    (r"head ?phones?|head ?sets?|over-?ears?|on-?ears?", "headphones"),
    (r"speakers?|sound ?bars?", "speakers"),
    (r"smart ?phones?|iphones?|phones?|galaxy", "phones"),
    (r"laptops?|macbooks?|notebook computers?", "laptops"),
    (r"sunglasses|shades", "sunglasses"),
    (r"hand ?bags?|backpacks?|clutch(?:es)?|totes?|crossbody|purses?|bags?", "bags"),
    (r"dress(?:es)?|gowns?|t-?shirts?|tees?|shirts?|polos?|jeans|chinos|pants|trousers|jackets?|sweaters?|"
     r"hoodies?|sweatshirts?|turtlenecks?|ponchos?|tops?", "clothing"),
    (r"belts?|wallets?|scarf|scarves", "accessories"),
]
_BUDGET = re.compile(r"(?:under|below|less than|up to|max(?:imum)?|within|no more than|cheaper than)\s*\$?\s*(\d+(?:\.\d+)?)"
                     r"|\$\s*(\d+(?:\.\d+)?)\s*(?:or less|max|tops)")
_REQUEST = re.compile(r"\b(need|want|looking for|show me|find|get me|buy|shopping for|do you have|any|"
                      r"recommend|suggest|i'?d like|searching for|gift|(?:can|could|may) i (?:get|have|see)|"
                      r"how about|what about|instead|actually|rather)\b")
CARRIED_BRANDS = {"nike": "Nike", "adidas": "Adidas", "converse": "Converse", "levi's": "Levi's", "levis": "Levi's",
                  "ray-ban": "Ray-Ban", "rayban": "Ray-Ban", "sony": "Sony", "apple": "Apple", "samsung": "Samsung",
                  "jbl": "JBL"}
# Well-known brands ShopSphere doesn't stock → "we don't carry X, here are similar".
NOT_CARRIED = ["gucci", "prada", "puma", "reebok", "new balance", "asics", "hoka", "under armour", "zara", "h&m",
               "lululemon", "bose", "beats", "dell", "lenovo", "google", "pixel", "oneplus", "fossil", "michael kors",
               "coach", "louis vuitton", "chanel", "balenciaga", "vans", "skechers", "timberland", "north face",
               "patagonia", "uniqlo", "garmin", "fitbit", "microsoft", "versace", "burberry", "hermes"]


def is_new_request(text: str) -> bool:
    """"wait, I need teal nike shoes of size 10" — a request for a product, not an
    answer to the question on screen, even though it contains a size."""
    return bool(search_intent(text)) and bool(_REQUEST.search(norm(text)))


# "no" / "not" rules out the next item only: "not a laptop", "don't want any
# sofa" — but in "no laptop tomatoes" the tomatoes are still wanted.
_NEGATION = re.compile(r"\b(?:no|not|don'?t|do not|never|without|instead of|rather than|except|other than)"
                       r"\s+(?:(?:a|an|any|the|some|more|other|want|need|like|show|me|get|buy|really)\s+){0,3}$")


def _wanted(pattern: str, t: str) -> Optional[re.Match]:
    """First match of pattern that the shopper isn't ruling out ("no laptop", "not a sofa")."""
    for m in re.finditer(rf"\b(?:{pattern})\b", t):
        if not _NEGATION.search(t[:m.start()]):
            return m
    return None


# Kinds of products ShopSphere doesn't sell at all (it sells clothing, shoes,
# accessories and electronics). Asking for these gets a plain "we don't sell
# that" instead of unrelated recommendations. Brand/colour words that clash
# (Apple, Orange) are deliberately left out.
NOT_SOLD = [
    (r"(?:dog|cat|pet) food|pet supplies", "pet supplies"),          # before groceries: "dog food" isn't groceries
    (r"grocer(?:y|ies)|food|tomato(?:es)?|potato(?:es)?|onions?|vegetables?|veggies|fruits?|bananas?|"
     r"milk|eggs?|bread|rice|flour|sugar|meat|chicken|fish|snacks?|chocolates?|coffee|cereal", "groceries or food"),
    (r"furniture|sofas?|couch(?:es)?|mattress(?:es)?|dining tables?|bookshel(?:f|ves)|wardrobes?", "furniture"),
    (r"medicines?|medications?|pills|pharmacy|vitamins|supplements", "medicine"),
    (r"cars?|bicycles?|motorcycles?|motorbikes?|scooters?", "vehicles"),
    (r"books?|novels?|magazines?", "books"),
    (r"toys?|lego|dolls?|board games?", "toys"),
    (r"makeup|make-up|lipsticks?|perfumes?|shampoos?|skincare|cosmetics", "beauty products"),
    (r"fridges?|refrigerators?|washing machines?|microwaves?|vacuum cleaners?|televisions?|tvs?", "home appliances"),
]


def not_sold(text: str) -> Optional[tuple[str, str]]:
    """("groceries or food", "tomatoes") when the shopper asks for something
    ShopSphere doesn't sell; None otherwise. "no tomatoes" doesn't count."""
    t = norm(text)
    for pattern, label in NOT_SOLD:
        m = _wanted(pattern, t)
        if m:
            return label, m.group(0)
    return None


def search_intent(text: str) -> Optional[dict]:
    """A clear product request ("running shoes under $150 for everyday running")
    parsed into search arguments, or None if the LLM should read it. Ruled-out
    kinds ("no laptop, headphones please") are skipped."""
    t = norm(text)
    category = next((cat for pattern, cat in _CATEGORY_WORDS if _wanted(pattern, t)), None)
    if not category:
        return None
    if t.endswith("?") or text.strip().endswith("?"):
        if not _REQUEST.search(t):        # "is it a good running shoe?" is a question, not a search
            return None
    m = _BUDGET.search(t)
    budget = float(m.group(1) or m.group(2)) if m else None
    brand = next((name for key, name in CARRIED_BRANDS.items() if re.search(rf"\b{re.escape(key)}\b", t)), None)
    if not brand:
        missing = next((b for b in NOT_CARRIED if re.search(rf"\b{re.escape(b)}\b", t)), None)
        brand = missing.title() if missing else None
    return {"query": text, "category": category, "max_price": budget, "gender": gender_of(text), "brand": brand}
