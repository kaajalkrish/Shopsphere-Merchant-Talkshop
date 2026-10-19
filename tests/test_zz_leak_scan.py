"""
CH-11 leak scan — named test_zz_… so it runs AFTER the whole suite (pytest runs files
alphabetically). By then every scenario has run against the shared test database:
no card number (PAN) or CVV may appear in any table, and no table may have a column
for one. ShopSphere only ever stores an opaque token, the network and the last 4.
"""
import re

import sqlalchemy
from sqlalchemy.orm import Session

from backend.db.init_db import get_engine

RAW_CARDS = ("4242424242424242", "4242 4242 4242 4242", "5555555555554444", "378282246310005", "6011111111111117")


def test_no_card_number_or_cvv_is_stored_anywhere():
    engine = get_engine()
    blob = []
    with Session(engine) as db:
        for table in sqlalchemy.inspect(engine).get_table_names():
            for row in db.execute(sqlalchemy.text(f"SELECT * FROM {table}")).fetchall():
                blob.append(" ".join(str(v) for v in row))
    text = "\n".join(blob)
    for raw in RAW_CARDS:
        assert raw not in text, raw
    # nothing that looks like a full card number with a valid Luhn checksum
    def luhn(n: str) -> bool:
        total, parity = 0, len(n) % 2
        for i, ch in enumerate(n):
            d = int(ch)
            if i % 2 == parity:
                d = d * 2 - 9 if d > 4 else d * 2
            total += d
        return total % 10 == 0
    candidates = {m for m in re.findall(r"(?<![\w.])\d{15,16}(?![\w.])", text) if luhn(m) and m[0] in "3456"}
    assert not candidates, sorted(candidates)[:5]
    cols = {c["name"].lower() for t in sqlalchemy.inspect(engine).get_table_names()
            for c in sqlalchemy.inspect(engine).get_columns(t)}
    assert not cols & {"pan", "card_number", "cvv", "cvc", "security_code"}
