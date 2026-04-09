-- =============================================================
-- Migration 005: Backtest run history
-- =============================================================
-- Persists every backtest run with full parameters, summary
-- metrics, and per-trade detail.  Enables:
--   - Run history / comparison across strategies and periods
--   - Per-symbol performance drill-down
--   - Equity curve storage per run
-- Safe to re-run (all statements use IF NOT EXISTS).
-- =============================================================

-- ── 1. Backtest runs ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS backtest_runs (
    id              SERIAL          PRIMARY KEY,
    strategy        VARCHAR(60)     NOT NULL,
    symbol          VARCHAR(20),                        -- NULL = all symbols
    period_label    VARCHAR(10),                        -- '1W','15D','1M','3M','6M','1Y','2Y','3Y','5Y','10Y','Max'
    start_date      DATE,
    end_date        DATE,
    capital         NUMERIC(14, 2)  NOT NULL DEFAULT 100000,
    risk_pct        NUMERIC(5, 2)   NOT NULL DEFAULT 1.5,

    -- Summary metrics (mirrors BacktestEngine._calculate_metrics)
    total_trades    INTEGER         NOT NULL DEFAULT 0,
    win_count       INTEGER         NOT NULL DEFAULT 0,
    loss_count      INTEGER         NOT NULL DEFAULT 0,
    win_rate        NUMERIC(6, 2),                      -- %
    total_pnl       NUMERIC(14, 2),
    total_pnl_pct   NUMERIC(8, 2),
    avg_pnl_trade   NUMERIC(14, 2),
    max_drawdown    NUMERIC(14, 2),
    max_drawdown_pct NUMERIC(8, 2),
    sharpe_ratio    NUMERIC(10, 4),
    profit_factor   NUMERIC(10, 4),
    best_trade      NUMERIC(14, 2),
    worst_trade     NUMERIC(14, 2),
    avg_hold_days   NUMERIC(6, 2),

    -- Metadata
    run_duration_ms INTEGER,                            -- wall-clock time in ms
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bt_runs_strategy
    ON backtest_runs (strategy);

CREATE INDEX IF NOT EXISTS idx_bt_runs_created
    ON backtest_runs (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_bt_runs_symbol
    ON backtest_runs (symbol);


-- ── 2. Backtest trades ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS backtest_trades (
    id              SERIAL          PRIMARY KEY,
    run_id          INTEGER         NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    symbol          VARCHAR(20)     NOT NULL,
    entry_date      DATE            NOT NULL,
    exit_date       DATE,
    entry_price     NUMERIC(14, 4),
    exit_price      NUMERIC(14, 4),
    stop_loss       NUMERIC(14, 4),
    target          NUMERIC(14, 4),
    shares          INTEGER,
    pnl             NUMERIC(14, 2),
    pnl_pct         NUMERIC(8, 2),
    exit_reason     VARCHAR(50),
    hold_days       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_bt_trades_run
    ON backtest_trades (run_id);

CREATE INDEX IF NOT EXISTS idx_bt_trades_symbol
    ON backtest_trades (symbol);

CREATE INDEX IF NOT EXISTS idx_bt_trades_entry_date
    ON backtest_trades (entry_date DESC);


-- ── 3. Equity curve per run ──────────────────────────────────
CREATE TABLE IF NOT EXISTS backtest_equity_curve (
    run_id          INTEGER         NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    curve_date      DATE            NOT NULL,
    equity          NUMERIC(14, 2)  NOT NULL,
    PRIMARY KEY (run_id, curve_date)
);


-- ── 4. Per-symbol summary per run ───────────────────────────
CREATE TABLE IF NOT EXISTS backtest_per_symbol (
    run_id          INTEGER         NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    symbol          VARCHAR(20)     NOT NULL,
    total_trades    INTEGER,
    win_count       INTEGER,
    loss_count      INTEGER,
    win_rate        NUMERIC(6, 2),
    total_pnl       NUMERIC(14, 2),
    total_pnl_pct   NUMERIC(8, 2),
    sharpe_ratio    NUMERIC(10, 4),
    profit_factor   NUMERIC(10, 4),
    max_drawdown    NUMERIC(14, 2),
    PRIMARY KEY (run_id, symbol)
);


INSERT INTO schema_migrations (version, description)
VALUES ('005', 'Backtest run history: backtest_runs, backtest_trades, backtest_equity_curve, backtest_per_symbol')
ON CONFLICT (version) DO NOTHING;
