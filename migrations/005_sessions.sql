-- Login sessions for the web app.
--   token_hash : SHA-256 of the random cookie token. The token itself is
--                never stored, so a leaked table can't be replayed as cookies.
--   expires_at : sessions end after SESSION_DAYS; logout deletes the row.

CREATE TABLE sessions (
    token_hash BYTEA PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_sessions_user ON sessions (user_id);
