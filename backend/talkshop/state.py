"""
Conversation stages and per-session state.

Stages follow the plan's §6.2 flow. Each stage lists what may happen next;
the orchestrator refuses anything else, so the LLM can't skip ahead (for
example, straight to payment).

State is in-process (like the older chat session state): enough for a demo
session, and the panel restores itself from `transcript` after a page change.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Stage(str, Enum):
    GREETING = "GREETING"
    SEARCHING = "SEARCHING"
    RECOMMENDED = "RECOMMENDED"            # top 3 shown — waiting for the shopper to choose
    PRODUCT_SELECTED = "PRODUCT_SELECTED"
    ASK_SIZE = "ASK_SIZE"
    ASK_COLOR = "ASK_COLOR"
    VARIANT_CONFIRMED = "VARIANT_CONFIRMED"
    IN_CART = "IN_CART"
    OFFER_CHECKOUT = "OFFER_CHECKOUT"
    CHECKOUT_DETAILS = "CHECKOUT_DETAILS"  # address/card missing — the secure forms collect them, not the chat
    AWAITING_CONSENT = "AWAITING_CONSENT"  # review card shown — only GO AHEAD can pay
    PAYING = "PAYING"
    ORDER_CONFIRMED = "ORDER_CONFIRMED"


# The journey stepper shown in the panel header.
# Customer-friendly progress (Part 2): Find → Choose → Review → Approve → Done
STEPPER = {
    Stage.GREETING: "Find", Stage.SEARCHING: "Find", Stage.RECOMMENDED: "Choose",
    Stage.PRODUCT_SELECTED: "Choose", Stage.ASK_SIZE: "Choose", Stage.ASK_COLOR: "Choose",
    Stage.VARIANT_CONFIRMED: "Choose", Stage.IN_CART: "Choose", Stage.OFFER_CHECKOUT: "Choose",
    Stage.CHECKOUT_DETAILS: "Review", Stage.AWAITING_CONSENT: "Review", Stage.PAYING: "Approve",
    Stage.ORDER_CONFIRMED: "Done",
}

# What each stage allows next (plan §6.2). A new search is allowed from any
# stage except while paying; "answer" (a question mid-flow) never changes stage.
ALLOWED = {
    Stage.GREETING: {"search", "answer", "select_product"},
    Stage.SEARCHING: {"answer"},
    Stage.RECOMMENDED: {"select_product", "search", "answer"},
    Stage.PRODUCT_SELECTED: {"choose_option", "search", "answer", "select_product"},
    Stage.ASK_SIZE: {"choose_option", "search", "answer", "select_product"},
    Stage.ASK_COLOR: {"choose_option", "search", "answer", "select_product"},
    Stage.VARIANT_CONFIRMED: {"search", "answer"},
    Stage.IN_CART: {"checkout", "search", "answer", "select_product"},
    Stage.OFFER_CHECKOUT: {"checkout", "keep_shopping", "search", "answer", "select_product"},
    Stage.CHECKOUT_DETAILS: {"cancel_checkout", "answer", "search"},
    Stage.AWAITING_CONSENT: {"update_checkout", "cancel_checkout", "answer", "search"},
    Stage.PAYING: set(),
    Stage.ORDER_CONFIRMED: {"search", "answer", "select_product"},
}
# Asking about an order already placed never changes where the shopper is (Phase 11).
for _stage, _allowed in ALLOWED.items():
    if _stage is not Stage.PAYING:
        _allowed.add("track_order")


@dataclass
class Session:
    session_id: str
    user_id: str
    stage: Stage = Stage.GREETING
    page: dict = field(default_factory=dict)
    last_query: Optional[str] = None
    shown: list[str] = field(default_factory=list)          # product ids of the current top 3
    product_id: Optional[str] = None                        # selected product
    size: Optional[str] = None
    color: Optional[str] = None
    sku: Optional[str] = None
    line_ids: list[str] = field(default_factory=list)       # cart lines Talkshop added in this conversation
    added_qty: dict[str, int] = field(default_factory=dict)  # how many of each line Talkshop added
    checkout_id: Optional[str] = None
    # CH-4: the invoice the shopper was last shown ({checkout_id, checkout_version, final_amount, currency}).
    # "Approve and pay" approves exactly this; if the order changed since, nothing is charged.
    shown_invoice: Optional[dict] = None
    order_id: Optional[str] = None
    is_visitor: bool = False                                # not logged in yet (Phase 8)
    access_ok: bool = False                                 # signed-in customer: may check out (CH-2; any issuer)
    wants: dict = field(default_factory=dict)               # size/colour asked for while searching — kept across picks
    history: list[dict] = field(default_factory=list)       # [{"role": "user"|"assistant", "text": ...}] for the LLM
    transcript: list[dict] = field(default_factory=list)    # every event sent, so the panel can rebuild itself

    def allows(self, action: str) -> bool:
        return action in ALLOWED[self.stage]

    def reset_selection(self):
        self.product_id = self.size = self.color = self.sku = None


_sessions: dict[tuple[str, str], Session] = {}


def get(user_id: str, session_id: str) -> Session:
    key = (user_id, session_id)
    if key not in _sessions:
        _sessions[key] = Session(session_id=session_id, user_id=user_id)
    return _sessions[key]


def peek(user_id: str, session_id: str) -> Optional[Session]:
    return _sessions.get((user_id, session_id))


def drop(user_id: str, session_id: str) -> None:
    _sessions.pop((user_id, session_id), None)


def rekey(old_user: str, new_user: str, line_map: Optional[dict[str, str]] = None) -> None:
    """A visitor logged in: their conversations now belong to the account.
    `line_map` (old cart line id → new) follows lines merged into the account's
    cart, so what Talkshop added still reaches checkout."""
    line_map = line_map or {}
    for (uid, sid) in [k for k in _sessions if k[0] == old_user]:
        sess = _sessions.pop((uid, sid))
        sess.user_id = new_user
        ids, qty = [], {}
        for lid in sess.line_ids:
            new_id = line_map.get(lid, lid)
            if new_id not in ids:
                ids.append(new_id)
            qty[new_id] = qty.get(new_id, 0) + sess.added_qty.get(lid, 1)
        sess.line_ids, sess.added_qty = ids, qty
        _sessions[(new_user, sid)] = sess
