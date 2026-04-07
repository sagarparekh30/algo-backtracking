-- =============================================================
-- Migration 003: Users & Subscription System
-- =============================================================

CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    username        VARCHAR(100) UNIQUE NOT NULL,
    email           VARCHAR(255) UNIQUE NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    role            VARCHAR(20)  NOT NULL DEFAULT 'user',   -- 'admin' | 'user'
    plan_type       VARCHAR(20),                            -- '1m' | '3m' | '6m' | '12m'
    plan_start      DATE,
    plan_expiry     DATE,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login      TIMESTAMPTZ,
    created_by      VARCHAR(100) DEFAULT 'admin'
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
CREATE INDEX IF NOT EXISTS idx_users_email    ON users (email);
CREATE INDEX IF NOT EXISTS idx_users_expiry   ON users (plan_expiry);

INSERT INTO schema_migrations (version, description)
VALUES ('003', 'Users and subscription system')
ON CONFLICT (version) DO NOTHING;
