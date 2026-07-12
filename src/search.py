"""
ThriftScout -- semantic + keyword search over listings.

search() embeds the query with Gemini (task_type="RETRIEVAL_QUERY") and calls
the hybrid_search() Postgres function to fuse keyword + semantic ranking.

Requires:  pip install google-genai pgvector psycopg2-binary python-dotenv
"""

import math
import os

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pgvector.psycopg2 import register_vector

load_dotenv()

DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    dbname=os.getenv("DB_NAME", "thriftly"),
    user=os.getenv("DB_USER", os.getenv("USER")),
    password=os.getenv("DB_PASSWORD", ""),
    port=os.getenv("DB_PORT", "5432"),
)

MODEL = "gemini-embedding-001"
EMBED_DIM = 1536

client = genai.Client()  # reads GEMINI_API_KEY from the environment


def _embed_query(text):
    """Embed a single search query -> 1536-dim unit vector."""
    result = client.models.embed_content(
        model=MODEL,
        contents=text,
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY",
            output_dimensionality=EMBED_DIM,
        ),
    )
    values = result.embeddings[0].values
    norm = math.sqrt(sum(x * x for x in values))
    return [x / norm for x in values] if norm else values


def search(query, match_count=5):
    """Run hybrid (keyword + semantic) search for `query`, returning the
    `match_count` best-matching listings as a list of dicts."""
    query_embedding = _embed_query(query)

    conn = psycopg2.connect(**DB)
    try:
        register_vector(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM hybrid_search(%s, %s::vector, %s);",
                (query, query_embedding, match_count),
            )
            return cur.fetchall()
    finally:
        conn.close()


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "vintage leather jacket"
    for row in search(q):
        print(row["id"], row["name"], row["price_cents"])
