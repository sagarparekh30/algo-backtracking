-- =============================================================
-- Migration 006: Pre-computed indicators & price action patterns
-- =============================================================
-- Stores daily technical indicators and candlestick patterns
-- alongside each OHLCV candle.  Pre-computing these avoids
-- recalculating on every strategy scan / backtest run.
--
-- Populated by: scripts/compute_indicators.py (scheduled daily)
-- Safe to re-run (all statements use IF NOT EXISTS).
-- =============================================================

-- ── 1. Daily technical indicators ───────────────────────────
CREATE TABLE IF NOT EXISTS equity_indicators (
    symbol              VARCHAR(20)     NOT NULL,
    trade_date          DATE            NOT NULL,

    -- ── Trend / Moving averages ──────────────────────────────
    ema_9               NUMERIC(14, 4),
    ema_20              NUMERIC(14, 4),
    ema_50              NUMERIC(14, 4),
    ema_200             NUMERIC(14, 4),
    sma_20              NUMERIC(14, 4),
    sma_50              NUMERIC(14, 4),
    sma_200             NUMERIC(14, 4),

    -- ── Momentum ────────────────────────────────────────────
    rsi_14              NUMERIC(6, 2),                  -- 0–100
    rsi_9               NUMERIC(6, 2),
    macd_line           NUMERIC(12, 6),
    macd_signal         NUMERIC(12, 6),
    macd_hist           NUMERIC(12, 6),
    stoch_k             NUMERIC(6, 2),                  -- %K
    stoch_d             NUMERIC(6, 2),                  -- %D (3-period SMA of %K)
    cci_20              NUMERIC(10, 4),                 -- Commodity Channel Index
    williams_r          NUMERIC(8, 4),                  -- Williams %R
    roc_10              NUMERIC(10, 4),                 -- Rate of Change 10-day

    -- ── Volatility ──────────────────────────────────────────
    atr_14              NUMERIC(14, 4),
    atr_pct             NUMERIC(8, 4),                  -- ATR / close × 100
    bb_upper            NUMERIC(14, 4),                 -- Bollinger upper (20,2)
    bb_middle           NUMERIC(14, 4),                 -- Bollinger middle (SMA20)
    bb_lower            NUMERIC(14, 4),                 -- Bollinger lower (20,2)
    bb_width            NUMERIC(8, 4),                  -- (upper-lower)/middle × 100
    bb_pct_b            NUMERIC(8, 4),                  -- (close-lower)/(upper-lower)
    keltner_upper       NUMERIC(14, 4),                 -- Keltner channel upper
    keltner_lower       NUMERIC(14, 4),                 -- Keltner channel lower
    squeeze             BOOLEAN,                        -- BB inside Keltner (TTM Squeeze)

    -- ── Trend strength ───────────────────────────────────────
    adx_14              NUMERIC(6, 2),                  -- 0–100, strength
    di_plus             NUMERIC(6, 2),                  -- +DI
    di_minus            NUMERIC(6, 2),                  -- -DI
    supertrend          NUMERIC(14, 4),
    supertrend_dir      SMALLINT,                       -- 1=bullish, -1=bearish

    -- ── Volume ──────────────────────────────────────────────
    vol_sma_20          NUMERIC(18, 2),
    vol_ratio           NUMERIC(8, 4),                  -- volume / vol_sma_20
    obv                 NUMERIC(18, 0),                 -- On-Balance Volume
    vwap_approx         NUMERIC(14, 4),                 -- (H+L+C)/3 × vol cumsum / vol cumsum

    -- ── Price action / derived ───────────────────────────────
    prev_close          NUMERIC(14, 4),
    gap_pct             NUMERIC(8, 4),                  -- (open - prev_close)/prev_close × 100
    daily_range_pct     NUMERIC(8, 4),                  -- (high - low)/close × 100
    body_pct            NUMERIC(8, 4),                  -- |close-open|/close × 100
    upper_wick_pct      NUMERIC(8, 4),                  -- upper wick as % of range
    lower_wick_pct      NUMERIC(8, 4),                  -- lower wick as % of range

    -- ── 52-week levels ───────────────────────────────────────
    high_52w            NUMERIC(14, 4),
    low_52w             NUMERIC(14, 4),
    pct_from_52w_high   NUMERIC(8, 2),                  -- negative = below 52w high
    pct_from_52w_low    NUMERIC(8, 2),                  -- positive = above 52w low

    -- ── Relative strength vs index ───────────────────────────
    rs_20               NUMERIC(8, 4),                  -- return_20d / nifty_return_20d
    rs_50               NUMERIC(8, 4),

    computed_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    PRIMARY KEY (symbol, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_indicators_date
    ON equity_indicators (trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_indicators_rsi
    ON equity_indicators (rsi_14);

CREATE INDEX IF NOT EXISTS idx_indicators_adx
    ON equity_indicators (adx_14);

CREATE INDEX IF NOT EXISTS idx_indicators_vol_ratio
    ON equity_indicators (vol_ratio DESC);


-- ── 2. Price action / candlestick patterns ──────────────────
CREATE TABLE IF NOT EXISTS price_action_patterns (
    id              SERIAL          PRIMARY KEY,
    symbol          VARCHAR(20)     NOT NULL,
    trade_date      DATE            NOT NULL,

    -- Pattern identification
    pattern         VARCHAR(50)     NOT NULL,           -- 'hammer','engulfing','doji','morning_star', etc.
    direction       VARCHAR(10)     NOT NULL,           -- 'bullish','bearish','neutral'
    strength        SMALLINT        NOT NULL DEFAULT 1, -- 1=weak, 2=moderate, 3=strong

    -- Context at pattern detection
    trend_context   VARCHAR(10),                        -- 'uptrend','downtrend','sideways'
    near_support    BOOLEAN         DEFAULT FALSE,
    near_resistance BOOLEAN         DEFAULT FALSE,
    volume_confirm  BOOLEAN         DEFAULT FALSE,      -- volume > 1.5x avg on pattern bar

    -- Signal quality
    atr_at_signal   NUMERIC(14, 4),
    close_at_signal NUMERIC(14, 4),

    computed_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    UNIQUE (symbol, trade_date, pattern)
);

CREATE INDEX IF NOT EXISTS idx_patterns_symbol_date
    ON price_action_patterns (symbol, trade_date DESC);

CREATE INDEX IF NOT EXISTS idx_patterns_pattern
    ON price_action_patterns (pattern);

CREATE INDEX IF NOT EXISTS idx_patterns_direction
    ON price_action_patterns (direction);

CREATE INDEX IF NOT EXISTS idx_patterns_date
    ON price_action_patterns (trade_date DESC);


-- ── 3. Daily market summary (Nifty-level context) ───────────
CREATE TABLE IF NOT EXISTS market_daily_summary (
    trade_date          DATE        PRIMARY KEY,

    -- Index levels
    nifty_open          NUMERIC(10, 2),
    nifty_high          NUMERIC(10, 2),
    nifty_low           NUMERIC(10, 2),
    nifty_close         NUMERIC(10, 2),
    nifty_volume        BIGINT,

    -- Derived
    nifty_return_1d     NUMERIC(8, 4),                  -- % change
    nifty_return_5d     NUMERIC(8, 4),
    nifty_return_20d    NUMERIC(8, 4),
    nifty_atr_14        NUMERIC(10, 4),

    -- Breadth
    advances            INTEGER,                        -- stocks above prev close
    declines            INTEGER,
    unchanged           INTEGER,
    advance_decline     NUMERIC(8, 4),                  -- advances/declines ratio

    -- Regime (from existing regime detector)
    regime              VARCHAR(10),                    -- 'Bull','Bear','Neutral'
    regime_score        NUMERIC(6, 2),

    computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_market_summary_date
    ON market_daily_summary (trade_date DESC);


INSERT INTO schema_migrations (version, description)
VALUES ('006', 'Pre-computed indicators: equity_indicators, price_action_patterns, market_daily_summary')
ON CONFLICT (version) DO NOTHING;
