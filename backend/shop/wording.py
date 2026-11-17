"""
Neutral wording for what Claude sees and for the live trace.

The payment engine, gateway, provider page and profile are demo services, and their own labels say
so ("Simulated card issuer", "Visa •••• 4242 (sandbox)", "Mock gateway / PSP", "(simulated
passkey; no issuer contacted)"). For the connector and the trace page those labels are rewritten
to name each part by its role ("Card issuer", "Visa •••• 4242", "Payment gateway / PSP"), so the
demo reads like the product. Nothing else changes: the same services run, and nothing here claims
a real bank, card network or wallet was contacted. The website and emails keep their own wording,
and the README says this is a demo environment.

neutral(text) rewrites one string; neutral_value(obj) every string inside dicts and lists (keys
and ids are left alone: a word is only touched when it stands on its own, so "sandbox_chat" or
"sbx_a1…" never change).
"""
import json
import re
from typing import Any

_WORDS = r"simulated|simulator|sandbox|fictional|mock|demo|test cards?|local fixture|not a real|made-up"

# Whole phrases first, then a demo word inside brackets drops the brackets, then single words.
_PHRASES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"Simulated \(in-process\)"), "In-process call"),
    (re.compile(r"(?i)\b(?:fictional demo profile|demo agent profile|demo guest wallet)\b"), "Saved shopper profile"),
    (re.compile(r"(?i)\bsimulated passkey authentication\b"), "Passkey authentication"),
    (re.compile(r"(?i)\bmock entitlement rules\b"), "Entitlement rules"),
    (re.compile(r"(?i)\bmock gateway\b"), "Payment gateway"),
    (re.compile(r"(?i)\bsimulated psp tokens\b"), "PSP tokens"),
    (re.compile(r"\bVIC simulator\b"), "VIC service"),
    (re.compile(r"\bSIMULATED payment verification\b"), "Payment verification"),
    (re.compile(r"^SIMULATED$"), "Hosted payment page"),
    (re.compile(r"\bSIMULATED\b"), "Payment provider"),
    (re.compile(r"(?i)\bsimulated route\b"), "Route"),
    (re.compile(r"(?i)\bdemo outbox\b"), "outbox"),
    (re.compile(r"(?i)\bdemo offer\b"), "Offer"),
    (re.compile(rf"(?i)\s*\([^()]*\b(?:{_WORDS})\b[^()]*\)"), ""),
    (re.compile(r"(?i)[,;]\s*(?:simulated|sandbox)\b(?=\s*(?:[.,;)]|$))"), ""),
    (re.compile(r"(?i)\s+\b(?:simulated|sandbox)\b(?=\s*(?:[.,;)]|$))"), ""),
]
_ADJECTIVE = re.compile(r"\b(Simulated|simulated|Sandbox|sandbox|Mock|mock|Fictional|fictional|Demo|demo)\s+(\w)")


def _drop_adjective(m: re.Match) -> str:
    word, nxt = m.group(1), m.group(2)
    return nxt.upper() if word[0].isupper() else nxt


def neutral(text: Any) -> Any:
    if not isinstance(text, str) or not text:
        return text
    for pattern, repl in _PHRASES:
        text = pattern.sub(repl, text)
    text = _ADJECTIVE.sub(_drop_adjective, text)
    return re.sub(r"(?<=\S)  +", " ", text)


def neutral_value(value: Any) -> Any:
    if isinstance(value, str):
        return neutral(value)
    if isinstance(value, list):
        return [neutral_value(v) for v in value]
    if isinstance(value, dict):
        return {k: neutral_value(v) for k, v in value.items()}
    return value


def neutral_json_text(text: str) -> str:
    """A tool result's text: JSON is rewritten value by value (never breaking it), plain text as is."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return neutral(text)
    return json.dumps(neutral_value(data), ensure_ascii=False)
