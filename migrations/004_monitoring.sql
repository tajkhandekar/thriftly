-- Monitoring: re-run saved searches on their interval and notify users about
-- new matches and price drops.

-- When the monitor last *tried* a search. Due-ness uses the later of this and
-- last_refreshed_at, so a search that keeps failing waits a full interval
-- before the next attempt instead of being retried on every tick.
ALTER TABLE searches ADD COLUMN last_attempted_at TIMESTAMPTZ;

-- ============================================================
-- listing_grades
--   Every model grade for (search, listing), including ones below the match
--   threshold, so a re-run only grades listings it hasn't seen. Without it,
--   each monitoring tick would re-grade the same candidates.
-- ============================================================
CREATE TABLE listing_grades (
    search_id  INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    score      REAL NOT NULL,
    graded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (search_id, listing_id)
);

-- ============================================================
-- notifications
--   One row per thing worth telling a user. sent_at NULL = not delivered yet;
--   the sender batches a user's unsent rows into one digest.
--   UNIQUE(search, listing, kind, price) : the same match or the same drop
--   to the same price is never queued twice.
-- ============================================================
CREATE TABLE notifications (
    id              BIGSERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    search_id       INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    listing_id      INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('new_match', 'price_drop')),
    old_price_cents INTEGER,             -- price_drop only
    price_cents     INTEGER NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at         TIMESTAMPTZ,
    CONSTRAINT uq_notification UNIQUE (search_id, listing_id, kind, price_cents)
);

CREATE INDEX idx_notifications_unsent
    ON notifications (user_id)
    WHERE sent_at IS NULL;

-- monitor: active searches ordered by the later of the two timestamps
DROP INDEX IF EXISTS idx_searches_refresh;
CREATE INDEX idx_searches_due
    ON searches ((greatest(last_refreshed_at, last_attempted_at)))
    WHERE is_active;
