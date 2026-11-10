"""
Gemini embeddings for Thriftly (gemini-embedding-001, 1536 dims, L2-normalized).

Queries and listings must be embedded with the same model and dimensionality,
using the matching task types (RETRIEVAL_QUERY vs RETRIEVAL_DOCUMENT), or the
semantic half of hybrid search compares incompatible vectors.

  python src/embeddings.py           # embed listings that have no embedding yet
  python src/embeddings.py --all     # re-embed every listing (e.g. after
                                     # changing listing_text() or the model)
"""

import math

import numpy as np
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pgvector.psycopg2 import register_vector
from psycopg2.extras import execute_values

from db import connect

load_dotenv()

MODEL = "gemini-embedding-001"
EMBED_DIM = 1536
BATCH_SIZE = 100  # max texts per embed_content request

_client = None


def _get_client():
    """Create the Gemini client on first use (reads GEMINI_API_KEY)."""
    global _client
    if _client is None:
        _client = genai.Client()
    return _client


def _normalize(values):
    # Gemini only returns unit vectors at 3072 dims; truncated ones need
    # normalizing so cosine distance behaves.
    norm = math.sqrt(sum(x * x for x in values))
    return [x / norm for x in values] if norm else list(values)


def _embed(texts, task_type):
    result = _get_client().models.embed_content(
        model=MODEL,
        contents=texts,
        config=types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=EMBED_DIM,
        ),
    )
    return [_normalize(e.values) for e in result.embeddings]


def embed_query(text):
    """Embed one search query -> 1536-dim unit vector."""
    return _embed([text], "RETRIEVAL_QUERY")[0]


def embed_documents(texts):
    """Embed listing texts in batches -> list of 1536-dim unit vectors."""
    vectors = []
    for i in range(0, len(texts), BATCH_SIZE):
        vectors.extend(_embed(texts[i:i + BATCH_SIZE], "RETRIEVAL_DOCUMENT"))
    return vectors


def listing_text(name, condition, description):
    """The text a listing is embedded from. Changing this means re-running
    with --all so old and new listings stay comparable."""
    parts = [name]
    if condition:
        parts.append(f"Condition: {condition}")
    if description:
        parts.append(description)
    return "\n".join(parts)


# Write a batch of embeddings in one statement. A row is only updated if its
# text still matches what was embedded: if a concurrent upsert changed it
# (and cleared the embedding), we skip it and it stays NULL for the next pass
# instead of getting a vector of its old text.
_WRITE_BATCH = """
UPDATE listings AS l
SET listing_embedding = v.embedding
FROM (VALUES %s) AS v (id, name, condition, description, embedding)
WHERE l.id = v.id
  AND (l.name, l.condition, l.description)
      IS NOT DISTINCT FROM (v.name, v.condition, v.description);
"""
_WRITE_TEMPLATE = "(%s, %s::text, %s::text, %s::text, %s::vector)"


def embed_pending(reembed_all=False):
    """Embed every active listing whose listing_embedding is NULL (new
    listings, or ones whose text changed on upsert), or every active listing
    with reembed_all. Each batch is committed on its own, so a Gemini error
    keeps the batches already done. Returns how many rows were written."""
    conn = connect()
    try:
        register_vector(conn)
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, condition, description FROM listings "
                "WHERE is_active"
                + ("" if reembed_all else " AND listing_embedding IS NULL")
                + " ORDER BY id;"
            )
            rows = cur.fetchall()

        written = 0
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            vectors = embed_documents([listing_text(n, c, d) for _, n, c, d in batch])
            with conn, conn.cursor() as cur:
                execute_values(
                    cur, _WRITE_BATCH,
                    [(*row, np.array(vec)) for row, vec in zip(batch, vectors)],
                    template=_WRITE_TEMPLATE,
                    page_size=BATCH_SIZE,
                )
                written += cur.rowcount
        return written
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Embed listings with Gemini.")
    parser.add_argument("--all", action="store_true",
                        help="re-embed every active listing, not just missing ones")
    args = parser.parse_args()
    print(f"Embedded {embed_pending(reembed_all=args.all)} listing(s).")
