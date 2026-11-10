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

## Running the web app

```bash
python src/migrate.py                                  # includes 005_sessions
uvicorn api:app --app-dir src --port 8000              # add --reload while developing
```

Open http://localhost:8000, create an account, and describe what you're looking for. To look at the UI without spending eBay or Gemini calls, `python src/seed_demo.py` creates a demo account (`demo@thriftly.local`, dev only; the password is in the script) with a finished search built from listings already in the catalog. API docs are at `/api/docs`.

Run `python src/monitor.py --loop` alongside the web app so watched searches get rechecked and alerts go out.

## Environment variables

| Variable | Used by | Purpose |
|---|---|---|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | everything (`src/db.py`) | Postgres connection |
| `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` | `ebay_client.py` | Browse API app token |
| `EBAY_VERIFICATION_TOKEN`, `EBAY_ENDPOINT_URL` | `ebay_deletion_endpoint.py` | Account-deletion handshake |
| `GEMINI_API_KEY` | `llm.py`, `embeddings.py` | Gemini embeddings and the agent model |
| `AGENT_MODEL` | `llm.py` | Agent model (default `gemini-3.5-flash`) |
| `AGENT_RPM` | `llm.py` | Max agent model calls per minute across threads (default 5, the free tier) |
| `COOKIE_SECURE` | `api.py` | `true` when served over HTTPS (session cookie gets `Secure`) |
| `SESSION_DAYS` | `auth.py` | Session lifetime (default 30) |
| `DB_POOL_MAX` | `db.py` | Pooled connections for the web app (default 10) |
| `EMAIL_BACKEND` | `notify.py` | `console` (default, print only), `file` (write `.eml` to `outbox/`) or `smtp` |
| `EMAIL_FROM`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` | `notify.py` | SMTP delivery (STARTTLS) |

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
| `python agent.py "used patagonia fleece under \$60"` | Save a search and run the agent: parse → eBay → hybrid search → grade → broaden if needed |
| `python agent.py --rerun 12` | Re-run saved search 12 with its saved plan (no re-parse) |
| `python monitor.py` | One monitoring tick: re-run due searches, queue alerts, send digests |
| `python monitor.py --loop --every 300 --deactivate-stale` | Keep monitoring every 5 minutes and retire listings unseen for 7 days |
| `python notify.py --preview` | Print pending alert digests without sending or marking them |
| `python query_parser.py "nwt north face puffer under \$150"` | Show how a request is parsed into keywords and filters |
| `python run_eval.py --parse-only` | Score the parser against `eval/queries.jsonl` |
| `python run_eval.py --only q01 q19` | Full agent runs for some or all eval queries, with match, precision and latency metrics |
| `python explain_hybrid_search.py "black denim jacket"` | `EXPLAIN ANALYZE` of the hybrid search query |
| `python ebay_deletion_endpoint.py` | Standalone Flask version of the eBay account-deletion webhook (the web app also serves it at `/ebay/deletion`) |
| `python auth.py set-password you@example.com` | Set a password, e.g. to enable an account the CLI created; signs out its sessions |
| `python seed_demo.py` | Dev only: demo account and a finished demo search |

### How listings stay fresh

- **Upsert:** re-fetching a listing refreshes its title, description, price, condition, images and seller. A missing seller or empty image list doesn't overwrite a known one.
- **Embeddings:** if the title, condition or description changed, its embedding is cleared and recomputed on the next embed pass, which ingestion runs automatically.
- **Price history:** a row is recorded only when the price differs from the last one recorded.
- **Stale listings:** `--deactivate-stale` marks listings no fetch has seen for 7 days inactive, and search skips inactive listings. It doesn't run automatically: until the monitoring job re-queries every saved search, "not fetched" only means "not searched again", not "sold".

### The search agent

One run of a saved search (`agent.run_search`):

1. **Parse** the request into eBay keywords, a broader fallback, a description for semantic search, and hard filters (price range, `New` / `Pre-owned`). This happens on the first run only; the plan and the description's embedding are saved on `searches` and reused.
2. **Fetch** up to 50 eBay results, with the price filter applied by eBay, then upsert and embed them.
3. **Search** the whole catalog with `hybrid_search` and the filters.
4. **Grade** each new candidate from 0 to 1 with the agent model. A score of 0.6 or more counts as a match.
5. **Broaden** while there are fewer than 3 matches, least intrusive step first: broader keywords, then any condition, then price ±25%.

Matches go to `search_matches`. Every step, with its timing, goes to `agent_traces.tools`.

**Gemini quota:** each run makes about 2–5 calls to the agent model. The free tier allows 5 calls a minute and 20 a day per model, which is enough for a few test searches but not a full eval. Enable billing on the Gemini project and raise `AGENT_RPM` for real use.

### Web app and API

`src/api.py` (FastAPI) serves the JSON API, the frontend in `src/web/` (plain HTML, CSS and JavaScript, no build step) and `/ebay/deletion`.

- **Accounts:** passwords are hashed with scrypt. A session is a random token in an HttpOnly, SameSite=Lax cookie; only its SHA-256 is stored in `sessions`, so logging out or changing the password revokes it. CLI-created users (`!disabled`) can't log in until `auth.py set-password` is run for them.
- **Searches:** `POST /api/searches {query, watch}` saves the search and runs the agent in the background. The page polls `GET /api/searches/{id}` until `state` leaves `queued` or `running`. "Watching" is `searches.is_active`, which is what the monitor rechecks.
- **Other endpoints:** `PATCH` (watch and interval), `POST …/refresh`, `DELETE`, and `GET /api/listings/{id}/price-history`. Every endpoint only sees the signed-in user's own searches and their listings.
- **Speed:** requests share a pool of autocommit connections (`db.pooled()`). A new TLS connection to RDS costs about 0.7 s, so loading a search takes about 0.2 s pooled instead of about 2 s.

### Monitoring and alerts

`monitor.py` re-runs saved searches on their `requery_interval_minutes`, which defaults to 60:

- **Picking searches:** a search is due once its interval has passed since it last refreshed or last failed, so a failing search isn't retried on every tick. Each tick takes the oldest due searches, up to `--max-searches` (default 10).
- **No double runs:** a per-search Postgres advisory lock keeps overlapping ticks or extra workers from running the same search twice.
- **Cheap re-runs:** the saved plan, embedding and per-listing grades (`listing_grades`) are reused, so re-running an unchanged search makes no model calls.
- **What gets alerted:**
  - matches found for the first time in this run
  - price drops of at least `--min-drop-pct` (default 5%) on listings the search had already matched

  A search's first run queues nothing, because its first results aren't news. Each alert is queued at most once.
- **Digests:** `notify.py` sends each user one digest of everything pending and marks it sent only once delivered. A failed send stays queued for the next tick. With `EMAIL_BACKEND=smtp`, `.local` addresses (the dev and eval users) are skipped.

To run it from cron every 15 minutes:

```
*/15 * * * * cd /path/to/thriftly && python3 src/monitor.py --deactivate-stale >> monitor.log 2>&1
```

`--deactivate-stale` marks listings no fetch has seen for 7 days inactive. Only turn it on once the searches you care about are being monitored. Its first run will deactivate any old listings no active search fetches again.

## Layout

```
migrations/          numbered SQL migrations (schema, hybrid_search + filters, monitoring, sessions)
src/db.py            shared Postgres settings + connect()
src/migrate.py       migration runner
src/ebay_client.py   eBay Browse API client (cached app token)
src/embeddings.py    Gemini embeddings (query + listing) and the backfill command
src/ingestion_pipeline.py  eBay -> listings / price_history, then embed
src/search.py        hybrid_search() with optional filters
src/llm.py           Gemini client, rate limit + retries, structured output
src/query_parser.py  request -> keywords, description, filters
src/agent.py         the search agent (parse, fetch, search, grade, broaden, trace)
src/run_eval.py      eval runner for eval/queries.jsonl
src/monitor.py       monitoring job: due searches -> re-run -> alerts
src/notify.py        alert queue, digest emails, console/file/smtp delivery
src/api.py           web app: JSON API + frontend + eBay deletion webhook
src/auth.py          scrypt passwords, sessions, set-password command
src/web/             frontend (index.html, app.js, style.css)
src/seed_demo.py     dev-only demo account and search
src/ebay_deletion_endpoint.py  eBay account-deletion webhook
```
