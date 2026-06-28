"""
Parses an eBay Browse API response and loads it into the database
"""

import json
import os

import psycopg2
from psycopg2.extras import Json

from ebay_client import fetch_listings
from dotenv import load_dotenv

load_dotenv()

# Connection settings
DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    dbname=os.getenv("DB_NAME", "thriftly"),
    user=os.getenv("DB_USER", os.getenv("USER")),
    password=os.getenv("DB_PASSWORD", ""),
    port=os.getenv("DB_PORT", "5432"),
)


def load_mock(path="mock_response.json"):
    """Loads browse request."""
    with open(path) as f:
        return json.load(f)


def parse_items(response):
    """Parses eBay Browse item_summary/search response to rows shaped for listings table."""
    items = []
    for it in response.get("itemSummaries", []):
        price = it.get("price") or {}
        value = price.get("value")
        price_cents = int(round(float(value) * 100)) if value is not None else None

        main_image = (it.get("image") or {}).get("imageUrl")
        thumbs = it.get("thumbnailImages") or []
        image_urls = [main_image] if main_image else [
            t.get("imageUrl") for t in thumbs if t.get("imageUrl")
        ]

        items.append({
            "source": "ebay",
            "source_listing_id": it["itemId"],
            "name": it["title"],                          
            "description": it.get("shortDescription"),
            "price_cents": price_cents,
            "condition": it.get("condition"),
            "image_urls": image_urls, 
            "web_link": it["itemWebUrl"],                   
            "seller_username": (it.get("seller") or {}).get("username"),
        })
    return items


# UPSERT: insert new listings; if we've seen (source, source_listing_id) before,
# update the price/condition and mark it active again. Gets listing id
# so we can also log a price observation.
UPSERT = """
INSERT INTO listings
    (source, source_listing_id, name, description, price_cents,
     condition, image_urls, web_link, seller_username, is_active, fetched_at)
VALUES
    (%(source)s, %(source_listing_id)s, %(name)s, %(description)s, %(price_cents)s,
     %(condition)s, %(image_urls)s, %(web_link)s, %(seller_username)s, TRUE, now())
ON CONFLICT (source, source_listing_id) DO UPDATE SET
    price_cents = EXCLUDED.price_cents,
    condition   = EXCLUDED.condition,
    is_active   = TRUE,
    fetched_at  = now()
RETURNING id, price_cents;
"""


def save(items):
    conn = psycopg2.connect(**DB)
    try:
        with conn, conn.cursor() as cur:
            for it in items:
                if it["price_cents"] is None or it["source_listing_id"] is None:
                    continue  # skip malformed rows (price/id are required)
                row = dict(it, image_urls=Json(it["image_urls"]))
                cur.execute(UPSERT, row)
                listing_id, price_cents = cur.fetchone()
                # Record one price observation for the time-series.
                cur.execute(
                    "INSERT INTO price_history (listing_id, price_cents) VALUES (%s, %s);",
                    (listing_id, price_cents),
                )
        print(f"Upserted {len(items)} listings and recorded price history.")
    finally:
        conn.close()


def show(limit=20):
    conn = psycopg2.connect(**DB)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT name, price_cents, condition FROM listings "
                "ORDER BY id LIMIT %s;", (limit,)
            )
            print("\nListings now in the database:")
            for title, cents, cond in cur.fetchall():
                print(f"  ${cents / 100:>7.2f}  {(cond or ''):<14}  {title[:58]}")
    finally:
        conn.close()


def main():
    response = fetch_listings("black denim jacket", limit=5)
    items = parse_items(response)
    save(items)
    show()


if __name__ == "__main__":
    main()