"""
Dev-only: create a demo account with a finished demo search, so the web app
has something to show without spending eBay/Gemini calls.

  python src/seed_demo.py                      # demo search for "jacket"
  python src/seed_demo.py --match fleece

The demo user is on the reserved .local domain, so it never gets real email.
Credentials (dev only, never use on a public deployment):
"""

import argparse
import json

import auth
from db import connect

DEMO_EMAIL = "demo@thriftly.local"
DEMO_PASSWORD = "thriftly-demo-pass"

__doc__ += f"  email {DEMO_EMAIL}  password {DEMO_PASSWORD}\n"


def seed(match_word="jacket", query="vintage jacket under $60, secondhand"):
    try:
        user_id = auth.create_user(DEMO_EMAIL, DEMO_PASSWORD)["id"]
    except auth.AuthError:
        auth.set_password(DEMO_EMAIL, DEMO_PASSWORD)
        conn = connect()
        with conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s;", (DEMO_EMAIL,))
            user_id = cur.fetchone()[0]
        conn.close()

    parsed = {"keywords": match_word, "broad_keywords": match_word,
              "description": query, "min_price_cents": None,
              "max_price_cents": 6000, "conditions": ["Pre-owned"]}
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO searches (user_id, raw_query, parsed_params, is_active, last_refreshed_at) "
                "VALUES (%s, %s, %s, TRUE, now()) RETURNING id;",
                (user_id, query, json.dumps(parsed)),
            )
            search_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO search_matches (search_id, listing_id, relevance_score)
                SELECT %s, id, CASE WHEN row_number() OVER (ORDER BY price_cents) %% 3 = 0
                                    THEN 0.7 ELSE 1.0 END
                FROM listings
                WHERE is_active AND name ILIKE %s AND price_cents <= 6000
                ORDER BY price_cents LIMIT 12;
                """,
                (search_id, f"%{match_word}%"),
            )
            matched = cur.rowcount
            cur.execute(
                "INSERT INTO agent_traces (search_id, status, result_count, ended_at) "
                "VALUES (%s, %s, %s, now());",
                (search_id, "ok" if matched else "no_results", matched),
            )
        print(f"demo search {search_id}: {matched} matches for {match_word!r}; "
              f"sign in as {DEMO_EMAIL}")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed a demo account (dev only).")
    parser.add_argument("--match", default="jacket", help="word to pick catalog listings by")
    args = parser.parse_args()
    seed(args.match)
