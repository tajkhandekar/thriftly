"""
Thriftly -- EXPLAIN ANALYZE baseline for hybrid_search().

Embeds a real query (via embeddings.embed_query) and runs hybrid_search() under
EXPLAIN ANALYZE, so index usage (HNSW / GIN on listing_tsv) and timing can be
checked now and compared against after schema/index changes.
"""

import numpy as np
from dotenv import load_dotenv
from pgvector.psycopg2 import register_vector

from embeddings import embed_query
from db import connect

load_dotenv()


def explain_hybrid_search(query_text="black denim jacket", match_count=5):
    query_embedding = embed_query(query_text)

    conn = connect()
    try:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                "EXPLAIN ANALYZE SELECT * FROM hybrid_search(%s, %s::vector, %s);",
                (query_text, np.array(query_embedding), match_count),
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
