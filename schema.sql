-- Thriftly schema
-- PostgreSQL + pgvector. Embedding dim = 1536 (OpenAI text-embedding-3-small);

CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- users
-- ============================================================
CREATE TABLE users (
    id              SERIAL PRIMARY KEY,
    email           TEXT UNIQUE NOT NULL,
    hashed_password TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- searches
--   query_embedding : embed once at save time, reuse on every monitoring re-run
--   parsed_params   : agent-extracted hard filters (max price, size, brand...)
--   requery_interval_minutes / last_refreshed_at : drive the monitoring job
-- ============================================================
CREATE TABLE searches (
    id                       SERIAL PRIMARY KEY,
    user_id                  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    raw_query                TEXT NOT NULL,
    query_embedding          vector(1536),
    parsed_params            JSONB,
    is_active                BOOLEAN NOT NULL DEFAULT TRUE,
    requery_interval_minutes INTEGER NOT NULL DEFAULT 60,
    last_refreshed_at        TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- listings  (global: one row per real-world item, shared across searches)
--   UNIQUE(source, source_listing_id) : real-world identity, enables UPSERT-on-refresh
--   listing_tsv : generated full-text column
--   listing_embedding : semantic half of hybrid search
-- ============================================================
CREATE TABLE listings (
    id                SERIAL PRIMARY KEY,
    source            TEXT NOT NULL,                 -- ex. 'ebay', 'local', ...
    source_listing_id TEXT NOT NULL,
    name              TEXT NOT NULL,
    description       TEXT,
    image_urls        JSONB,
    web_link          TEXT,
    price_cents       INTEGER NOT NULL,
    condition         TEXT,
    seller_username   text,
    listing_tsv       tsvector GENERATED ALWAYS AS (
                          to_tsvector('english',
                              coalesce(name, '') || ' ' || coalesce(description, ''))
                      ) STORED,
    listing_embedding vector(1536),
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_listings_source UNIQUE (source, source_listing_id)
);

-- ============================================================
-- search_matches  (where searches and listings connect)
--   relevance_score : the computed match score, persisted so can be ranked
--                     without recomputing
-- ============================================================
CREATE TABLE search_matches (
    id              SERIAL PRIMARY KEY,
    search_id       INTEGER NOT NULL REFERENCES searches(id)  ON DELETE CASCADE,
    listing_id      INTEGER NOT NULL REFERENCES listings(id)  ON DELETE CASCADE,
    relevance_score REAL,
    surfaced_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_match UNIQUE (search_id, listing_id)
);

-- ============================================================
-- price_history
-- ============================================================
CREATE TABLE price_history (
    id          BIGSERIAL PRIMARY KEY,
    listing_id  INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    price_cents INTEGER NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- agent_traces
--   One row per agent run. 
--   search_id                    : access for results + raw query
--   result_count / broaden_count : success metrics for the eval set
--   status / error_message       : outcome
--   ended_at NULL                : run still in flight or crashed
-- ============================================================
CREATE TABLE agent_traces (
    id            SERIAL PRIMARY KEY,
    search_id     INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    result_count  INTEGER NOT NULL DEFAULT 0,
    broaden_count INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL CHECK (status IN ('ok', 'no_results', 'error')),
    error_message TEXT,
    tools         JSONB,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at      TIMESTAMPTZ
);

-- ============================================================
-- INDEXES
-- ============================================================

-- keyword search over listings
CREATE INDEX idx_listings_tsv
    ON listings USING GIN (listing_tsv);

-- semantic search over listings (HNSW, cosine distance)
CREATE INDEX idx_listings_embedding
    ON listings USING hnsw (listing_embedding vector_cosine_ops);

-- structured filter path (price / condition)
CREATE INDEX idx_listings_filter
    ON listings (price_cents, condition);

-- monitoring job: find active searches due for a re-query
CREATE INDEX idx_searches_refresh
    ON searches (last_refreshed_at)
    WHERE is_active;

-- price lookups: "latest price" and "price over time" per listing
CREATE INDEX idx_price_history_listing
    ON price_history (listing_id, observed_at DESC);
