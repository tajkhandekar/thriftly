"""
ThriftScout -- EXPLAIN ANALYZE baseline for hybrid_search().

Embeds a real query (via search._embed_query) and runs hybrid_search() under
EXPLAIN ANALYZE, so index usage (HNSW / GIN on listing_tsv) and timing can be
checked now and compared against after schema/index changes.
"""

import os

import psycopg2
from dotenv import load_dotenv

from search import DB, _embed_query

load_dotenv()


def explain_hybrid_search(query_text="black denim jacket", match_count=5):
    query_embedding = _embed_query(query_text)
    vector_literal = "[" + ",".join(str(x) for x in query_embedding) + "]"

    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "EXPLAIN ANALYZE SELECT * FROM hybrid_search(%s, %s::vector, %s);",
                (query_text, vector_literal, match_count),
            )
            plan = "\n".join(row[0] for row in cur.fetchall())
            print(plan)
            return plan
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "black denim jacket"
    explain_hybrid_search(q)
