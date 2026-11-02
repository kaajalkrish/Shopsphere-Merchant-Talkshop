"""
"earphone" is earbuds, never a phone: the request parser maps audio words to their own
kinds (earbuds / headphones / speakers), and catalog relevance matches tags as whole words
("phone" doesn't match inside "earphone").
"""
import pytest

from backend.talkshop import parse, tools


@pytest.mark.parametrize("text,kind", [
    ("I am looking for earphone", "earbuds"), ("earphones please", "earbuds"), ("ear phones under $100", "earbuds"),
    ("earbud", "earbuds"), ("show me airpods", "earbuds"),
    ("noise cancelling headphones", "headphones"), ("a gaming headset", "headphones"),
    ("a bluetooth speaker", "speakers"),
    ("I need a phone", "phones"), ("iphone", "phones"),
])
def test_audio_words_are_their_own_kind(text, kind):
    assert parse.search_intent(text)["category"] == kind


def search(text):
    intent = parse.search_intent(text)
    return tools.search_products(query=text, category=intent["category"], max_price=intent["max_price"])["products"]


def test_earphone_finds_earbuds_not_phones():
    found = search("I am looking for earphone")
    assert found and all(p["subcategory"] == "earbuds" for p in found)
    assert not any(p["category"] == "phones" for p in found)


def test_headphones_and_speakers_stay_in_their_kind():
    assert {p["subcategory"] for p in search("noise cancelling headphones")} == {"headphones"}
    assert {p["subcategory"] for p in search("a bluetooth speaker")} == {"speakers"}


def test_phones_still_find_phones():
    assert {p["category"] for p in search("I need a phone")} == {"phones"}
