"""
Turn a shopper's natural-language request into a structured search plan:
keywords for eBay and full-text search, a description for semantic search,
and hard filters (price range, condition).

  python src/query_parser.py "used patagonia fleece under \$60"
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

from llm import generate_structured

# Values hybrid_search() prefix-matches against eBay's graded conditions
# ("Pre-owned" matches "Pre-owned - Good", "New" matches "New with tags").
Condition = Literal["New", "Pre-owned"]


class ParsedQuery(BaseModel):
    keywords: str = Field(
        description="2-6 word search-engine query for the item: brand, model, "
                    "item type. No prices, conditions or filler words.")
    broad_keywords: str = Field(
        description="A shorter, more general version of keywords (1-3 words) "
                    "to fall back on if keywords finds too little.")
    description: str = Field(
        description="One sentence describing what the shopper wants, including "
                    "style, use and attributes, for semantic matching. No prices.")
    min_price_cents: Optional[int] = Field(
        description="Lowest acceptable price in US cents, or null.")
    max_price_cents: Optional[int] = Field(
        description="Highest acceptable price in US cents, or null.")
    conditions: Optional[list[Condition]] = Field(
        description="Allowed conditions, or null if the shopper didn't restrict it.")

    def filters(self):
        """Filters in the shape search.search() accepts."""
        return {
            "min_price_cents": self.min_price_cents,
            "max_price_cents": self.max_price_cents,
            "conditions": self.conditions or None,
        }


SYSTEM = """You turn a shopper's request for a secondhand item into a search plan.

Prices are integer US cents:
- "under $60", "less than $60", "max $60", "$60 or less", "nothing over 60 bucks" -> max_price_cents 6000
- "over $100", "at least $100" -> min_price_cents 10000
- "between $20 and $40" -> min 2000, max 4000
- "around $50" / "about $50" -> max_price_cents 6000 (20% over), min null
- no price mentioned -> both null

Conditions (use only "New" and "Pre-owned"):
- used, secondhand, thrifted, pre-loved, worn, vintage-and-used -> ["Pre-owned"]
- new, brand new, NWT, new with tags, unworn, sealed -> ["New"]
- not mentioned, or "new or used" -> null
"Vintage" alone describes style, not condition.

keywords must be what you'd type into eBay: fix typos and drop slang, prices,
conditions, sizes and filler ("looking for", "something", "cute", "rare",
"discontinued"). Every keyword must plausibly appear in a matching listing's
title, because keyword search requires all of them."""


def parse_query(raw_query):
    """Parse `raw_query` into a ParsedQuery with sane, consistent filters."""
    parsed = generate_structured(raw_query, ParsedQuery, system=SYSTEM)
    # Guard against inverted or non-positive bounds from the model.
    lo, hi = parsed.min_price_cents, parsed.max_price_cents
    if lo is not None and lo <= 0:
        parsed.min_price_cents = lo = None
    if hi is not None and hi <= 0:
        parsed.max_price_cents = hi = None
    if lo is not None and hi is not None and lo > hi:
        parsed.min_price_cents, parsed.max_price_cents = hi, lo
    if parsed.conditions is not None:
        parsed.conditions = sorted(set(parsed.conditions)) or None
    return parsed


if __name__ == "__main__":
    import sys

    print(parse_query(" ".join(sys.argv[1:]) or "used patagonia fleece under $60")
          .model_dump_json(indent=2))
