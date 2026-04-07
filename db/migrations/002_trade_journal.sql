-- Migration 002: Trade Journal
-- Tracks paper and live trades logged from the Execute tab.

CREATE TABLE IF NOT EXISTS trade_journal (
    id               SERIAL PRIMARY KEY,
    symbol           VARCHAR(50)     NOT NULL,
    strategy_tags    TEXT            NOT NULL DEFAULT '',   -- comma-separated strategy names
    entry_price      NUMERIC(14,4)   NOT NULL,
    stop_loss        NUMERIC(14,4),
    target           NUMERIC(14,4),
    entry_date       DATE            NOT NULL DEFAULT CURRENT_DATE,
    exit_date        DATE,
    exit_price       NUMERIC(14,4),
    status           VARCHAR(20)     NOT NULL DEFAULT 'open',  -- open | win | loss | stopped | cancelled
    trade_type       VARCHAR(10)     NOT NULL DEFAULT 'paper', -- paper | live
    notes            TEXT            NOT NULL DEFAULT '',
    buy_probability  NUMERIC(5,4),
    expected_return_pct NUMERIC(8,4),
    actual_return_pct   NUMERIC(8,4),
    quantity         INTEGER         NOT NULL DEFAULT 0,
    pnl              NUMERIC(14,2),
    created_at       TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_journal_symbol     ON trade_journal (symbol);
CREATE INDEX IF NOT EXISTS idx_journal_status     ON trade_journal (status);
CREATE INDEX IF NOT EXISTS idx_journal_entry_date ON trade_journal (entry_date DESC);
