# Thriftly

Natural-language secondhand search. Thriftly pulls listings from eBay, stores them in
Postgres, and ranks them with hybrid search (Postgres full-text + pgvector semantic
similarity, fused with reciprocal rank fusion).

## Requirements

- Python 3.11+
- PostgreSQL 15+ with the [pgvector](https://github.com/pgvector/pgvector) extension
- eBay developer application keys
- A Gemini API key (embeddings: `gemini-embedding-001`, 1536 dims)

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the values
```

### Database

Migrations live in `migrations/` as numbered SQL files and are applied by
`src/migrate.py`, which records each one in a `schema_migrations` table.

```bash
python src/migrate.py            # apply pending migrations
python src/migrate.py --status   # show applied / pending
```

If your database was created earlier by hand from the old `schema.sql` and
`hybrid_search.sql`, mark those two as already applied once (later migrations
stay pending and run normally):

```bash
python src/migrate.py --baseline 002
```

Only baseline what the database really has. If `hybrid_search()` was never
created, use `--baseline 001` instead.

To change the schema, add a new file such as `migrations/003_add_x.sql`. Don't edit
migrations that have already been applied.

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | everything (`src/db.py`) | Postgres connection |
| `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` | `ebay_client.py` | Browse API app token |
| `EBAY_VERIFICATION_TOKEN`, `EBAY_ENDPOINT_URL` | `ebay_deletion_endpoint.py` | Account-deletion handshake |
| `GEMINI_API_KEY` | `search.py` | Query embeddings |

## Running things

Run these from `src/`:

| Command | What it does |
|---|---|
| `python ingestion_pipeline.py "black denim jacket" -n 20` | Fetch eBay listings for a query, upsert them, and embed new or changed ones |
| `python ingestion_pipeline.py --deactivate-stale` | Mark listings no fetch has seen for 7 days inactive. Only use it once searches are refreshed regularly. |
| `python embeddings.py` | Embed any active listing that has no embedding (backfill) |
| `python embeddings.py --all` | Re-embed every active listing (after changing the model or `listing_text()`) |
| `python search.py "vintage leather jacket"` | Hybrid search over active listings |
| `python search.py "warm boots" --max-price 80 --condition pre-owned` | Same, with hard filters (`--min-price`, `--max-price`, `--condition`, `--source`; condition matches by prefix) |
| `python explain_hybrid_search.py "black denim jacket"` | `EXPLAIN ANALYZE` of the hybrid search query |
| `python ebay_deletion_endpoint.py` | Flask server for eBay marketplace account-deletion notifications (port 8000) |

### How listings stay fresh

- **Upsert:** re-fetching a listing refreshes its title, description, price, condition, images and seller. A missing seller or empty image list doesn't overwrite a known one.
- **Embeddings:** if the title, condition or description changed, its embedding is cleared and recomputed on the next embed pass, which ingestion runs automatically.
- **Price history:** a row is recorded only when the price differs from the last one recorded.
- **Stale listings:** `--deactivate-stale` marks listings no fetch has seen for 7 days inactive, and search skips inactive listings. It doesn't run automatically: until the monitoring job re-queries every saved search, "not fetched" only means "not searched again", not "sold".

## Layout

```
migrations/          numbered SQL migrations (schema, hybrid_search + filters)
src/db.py            shared Postgres settings + connect()
src/migrate.py       migration runner
src/ebay_client.py   eBay Browse API client (cached app token)
src/embeddings.py    Gemini embeddings (query + listing) and the backfill command
src/ingestion_pipeline.py  eBay -> listings / price_history, then embed
src/search.py        hybrid_search() with optional filters
src/ebay_deletion_endpoint.py  eBay account-deletion webhook
```
