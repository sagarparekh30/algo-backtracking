-- =============================================================
-- Migration 009: Add mobile column to users
-- =============================================================

ALTER TABLE users ADD COLUMN IF NOT EXISTS mobile VARCHAR(20);

CREATE INDEX IF NOT EXISTS idx_users_mobile ON users (mobile);

INSERT INTO schema_migrations (version, description)
VALUES ('009', 'Add mobile column to users')
ON CONFLICT (version) DO NOTHING;
