"""
Thriftly -- semantic + keyword search over listings.

search() embeds the query with Gemini (task_type="RETRIEVAL_QUERY") and calls
the hybrid_search() Postgres function to fuse keyword + semantic ranking,
optionally restricted by hard filters (price range, condition, source).

  python src/search.py "vintage leather jacket"
  python src/search.py "vintage leather jacket" --max-price 80 --condition Pre-owned
"""

import numpy as np
import psycopg2.extras
from pgvector.psycopg2 import register_vector

from db import connect
from embeddings import embed_query

# Filter keys search() accepts; they map 1:1 onto hybrid_search() arguments.
FILTER_KEYS = ("min_price_cents", "max_price_cents", "conditions", "sources")


def _as_array(embedding):
    """list / numpy array / pgvector Vector (what a vector column reads back
    as) -> 1-D float32 numpy array."""
    if hasattr(embedding, "to_numpy"):
        embedding = embedding.to_numpy()
    return np.asarray(embedding, dtype=np.float32)


def _validate_filters(filters):
    """Fail with a clear message instead of a Postgres type error."""
    for key in ("min_price_cents", "max_price_cents"):
        value = filters.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"{key} must be an int (cents) or None, got {value!r}")
    for key in ("conditions", "sources"):
        value = filters.get(key)
        if value is not None and (
            not isinstance(value, (list, tuple))
            or not all(isinstance(v, str) for v in value)
        ):
            raise ValueError(f"{key} must be a list of strings or None, got {value!r}")


def search(query, match_count=5, filters=None, query_embedding=None):
    """Run hybrid (keyword + semantic) search for `query`, returning the
    `match_count` best-matching active listings as a list of dicts.

    `filters` may contain any of FILTER_KEYS, e.g.
    {"max_price_cents": 8000, "conditions": ["Pre-owned"]}.
    Conditions match case-insensitively by prefix ("Pre-owned" matches
    "Pre-owned - Good"); see migrations/003_hybrid_search_filters.sql.
    Missing or None values mean "no constraint".

    `query` drives the keyword half. Pass `query_embedding` to drive the
    semantic half with a saved vector (e.g. searches.query_embedding)
    instead of embedding `query`.
    """
    filters = filters or {}
    unknown = set(filters) - set(FILTER_KEYS)
    if unknown:
        raise ValueError(f"unknown search filters: {sorted(unknown)}")

    _validate_filters(filters)

    if query_embedding is None:
        query_embedding = embed_query(query)

    conn = connect()
    try:
        register_vector(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM hybrid_search(
                    query_text      => %(query)s,
                    query_embedding => %(embedding)s::vector,
                    match_count     => %(match_count)s,
                    min_price_cents => %(min_price_cents)s,
                    max_price_cents => %(max_price_cents)s,
                    conditions      => %(conditions)s::text[],
                    sources         => %(sources)s::text[]
                );
                """,
                {
                    "query": query,
                    "embedding": _as_array(query_embedding),  # sent as a pgvector value
                    "match_count": match_count,
                    **{k: filters.get(k) for k in FILTER_KEYS},
                },
            )
            return cur.fetchall()
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Hybrid search over listings.")
    parser.add_argument("query", nargs="*", default=["vintage leather jacket"])
    parser.add_argument("-n", type=int, default=5, help="number of results")
    parser.add_argument("--min-price", type=float, help="dollars")
    parser.add_argument("--max-price", type=float, help="dollars")
    parser.add_argument("--condition", action="append",
                        help="allowed condition prefix (repeatable), e.g. Pre-owned")
    parser.add_argument("--source", action="append", help="e.g. ebay (repeatable)")
    args = parser.parse_args()

    def cents(dollars):
        return None if dollars is None else int(round(dollars * 100))

    results = search(
        " ".join(args.query),
        match_count=args.n,
        filters={
            "min_price_cents": cents(args.min_price),
            "max_price_cents": cents(args.max_price),
            "conditions": args.condition,
            "sources": args.source,
        },
    )
    for row in results:
        print(f"{row['id']:>5}  ${row['price_cents'] / 100:>8.2f}  "
              f"{(row['condition'] or ''):<12}  {row['name'][:60]}")
