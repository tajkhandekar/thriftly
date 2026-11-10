"""
Parses an eBay Browse API response, loads it into the database, and embeds
any listing that is new or whose text changed.

  python src/ingestion_pipeline.py "black denim jacket"
  python src/ingestion_pipeline.py --deactivate-stale   # see deactivate_stale()
"""

import json

from psycopg2.extras import Json

from ebay_client import fetch_listings
from db import connect
from embeddings import embed_pending
from dotenv import load_dotenv

load_dotenv()


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
# refresh everything eBay may have changed and mark it active again.
# If the embedded text (name / condition / description) changed, clear
# listing_embedding so embed_pending() re-embeds it.
UPSERT = """
INSERT INTO listings
    (source, source_listing_id, name, description, price_cents,
     condition, image_urls, web_link, seller_username, is_active, fetched_at)
VALUES
    (%(source)s, %(source_listing_id)s, %(name)s, %(description)s, %(price_cents)s,
     %(condition)s, %(image_urls)s, %(web_link)s, %(seller_username)s, TRUE, now())
ON CONFLICT (source, source_listing_id) DO UPDATE SET
    name              = EXCLUDED.name,
    description       = EXCLUDED.description,
    price_cents       = EXCLUDED.price_cents,
    condition         = EXCLUDED.condition,
    -- keep known values when a response omits them (seller_username is
    -- what eBay account-deletion requests are matched on)
    image_urls        = coalesce(nullif(EXCLUDED.image_urls, '[]'::jsonb),
                                 listings.image_urls),
    web_link          = EXCLUDED.web_link,
    seller_username   = coalesce(EXCLUDED.seller_username, listings.seller_username),
    listing_embedding = CASE
        WHEN (listings.name, listings.condition, listings.description)
             IS DISTINCT FROM
             (EXCLUDED.name, EXCLUDED.condition, EXCLUDED.description)
        THEN NULL
        ELSE listings.listing_embedding
    END,
    is_active         = TRUE,
    fetched_at        = now()
RETURNING id, price_cents;
"""

# Record a price observation only when it differs from the latest one
# (or there is none yet), so price_history holds changes, not every refresh.
RECORD_PRICE = """
INSERT INTO price_history (listing_id, price_cents)
SELECT %(listing_id)s, %(price_cents)s
WHERE %(price_cents)s IS DISTINCT FROM (
    SELECT price_cents FROM price_history
    WHERE listing_id = %(listing_id)s
    ORDER BY observed_at DESC, id DESC   -- same-transaction rows share now()
    LIMIT 1
);
"""

# Listings not seen by any fetch for this long are treated as sold or ended.
# Only meaningful once every active search is re-queried regularly (the
# Phase 3 monitoring job); until then "not fetched" just means "not searched
# again", so deactivate_stale() is never run automatically.
STALE_AFTER_DAYS = 7


def save(items):
    saved = 0
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            for it in items:
                if it["price_cents"] is None or it["source_listing_id"] is None:
                    continue  # skip malformed rows (price/id are required)
                row = dict(it, image_urls=Json(it["image_urls"]))
                cur.execute(UPSERT, row)
                listing_id, price_cents = cur.fetchone()
                cur.execute(
                    RECORD_PRICE,
                    {"listing_id": listing_id, "price_cents": price_cents},
                )
                saved += 1
        skipped = len(items) - saved
        print(f"Upserted {saved} listings (skipped {skipped} malformed).")
    finally:
        conn.close()


def deactivate_stale(days=STALE_AFTER_DAYS):
    """Mark listings no fetch has seen for `days` days as inactive, so search
    stops returning items that have probably sold. Returns the count.

    Run it only when the searches that own these listings are being
    refreshed (the monitoring job). Run ad hoc against a database that isn't
    refreshed, it deactivates everything older than `days`."""
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE listings SET is_active = FALSE "
                "WHERE is_active AND fetched_at < now() - make_interval(days => %s);",
                (days,),
            )
            return cur.rowcount
    finally:
        conn.close()


def ingest(query, limit=50):
    """Fetch eBay results for `query`, upsert them, then embed what's new."""
    save(parse_items(fetch_listings(query, limit=limit)))
    print(f"Embedded {embed_pending()} listing(s).")


def show(limit=20):
    conn = connect()
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
    import argparse

    parser = argparse.ArgumentParser(description="Ingest eBay listings.")
    parser.add_argument("query", nargs="*", default=["black denim jacket"])
    parser.add_argument("-n", type=int, default=5, help="listings to fetch")
    parser.add_argument("--deactivate-stale", action="store_true",
                        help=f"only mark listings unseen for {STALE_AFTER_DAYS}"
                             " days inactive (no fetch)")
    args = parser.parse_args()

    if args.deactivate_stale:
        print(f"Deactivated {deactivate_stale()} stale listing(s).")
        return
    ingest(" ".join(args.query), limit=args.n)
    show()


if __name__ == "__main__":
    main()