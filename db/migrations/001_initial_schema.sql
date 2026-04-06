-- =============================================================
-- Migration 001: Initial schema
-- =============================================================
-- Run once to create all required tables and indexes.
-- Safe to re-run (all statements use IF NOT EXISTS).
-- =============================================================

-- ── 1. Core OHLCV candle table ────────────────────────────────
CREATE TABLE IF NOT EXISTS equity_daily_candles_swing_trading (
    symbol      VARCHAR(50)     NOT NULL,
    trade_date  DATE            NOT NULL,
    open        NUMERIC(14, 4)  NOT NULL,
    high        NUMERIC(14, 4)  NOT NULL,
    low         NUMERIC(14, 4)  NOT NULL,
    close       NUMERIC(14, 4)  NOT NULL,
    volume      BIGINT          NOT NULL DEFAULT 0,
    source      VARCHAR(20)     NOT NULL DEFAULT 'UNKNOWN',
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    PRIMARY KEY (symbol, trade_date)
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_candles_symbol
    ON equity_daily_candles_swing_trading (symbol);

CREATE INDEX IF NOT EXISTS idx_candles_date
    ON equity_daily_candles_swing_trading (trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_candles_symbol_date
    ON equity_daily_candles_swing_trading (symbol, trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_candles_source
    ON equity_daily_candles_swing_trading (source);

-- ── 2. Migration tracking table ───────────────────────────────
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     VARCHAR(50)  PRIMARY KEY,
    applied_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    description TEXT
);

INSERT INTO schema_migrations (version, description)
VALUES ('001', 'Initial schema: equity_daily_candles_swing_trading')
ON CONFLICT (version) DO NOTHING;
