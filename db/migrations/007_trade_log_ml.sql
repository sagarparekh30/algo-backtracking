-- Migration 007: Trade Log for ML Retraining
-- Extends trade_journal with fields needed for model feedback loop.

-- Add ML-specific columns to trade_journal (idempotent with IF NOT EXISTS equivalent)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='trade_journal' AND column_name='signal_id') THEN
        ALTER TABLE trade_journal ADD COLUMN signal_id       VARCHAR(64);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='trade_journal' AND column_name='quality_score') THEN
        ALTER TABLE trade_journal ADD COLUMN quality_score   NUMERIC(6,2);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='trade_journal' AND column_name='regime') THEN
        ALTER TABLE trade_journal ADD COLUMN regime          VARCHAR(20);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='trade_journal' AND column_name='strategies_fired') THEN
        ALTER TABLE trade_journal ADD COLUMN strategies_fired TEXT;   -- JSON array
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='trade_journal' AND column_name='held_days') THEN
        ALTER TABLE trade_journal ADD COLUMN held_days       INTEGER;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='trade_journal' AND column_name='exit_reason') THEN
        ALTER TABLE trade_journal ADD COLUMN exit_reason     VARCHAR(64);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='trade_journal' AND column_name='pnl_pct') THEN
        ALTER TABLE trade_journal ADD COLUMN pnl_pct         NUMERIC(8,4);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='trade_journal' AND column_name='atr_at_entry') THEN
        ALTER TABLE trade_journal ADD COLUMN atr_at_entry    NUMERIC(14,4);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='trade_journal' AND column_name='liquidity_score') THEN
        ALTER TABLE trade_journal ADD COLUMN liquidity_score INTEGER;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='trade_journal' AND column_name='event_risk_status') THEN
        ALTER TABLE trade_journal ADD COLUMN event_risk_status VARCHAR(10);  -- CLEAR/CAUTION/BLOCK
    END IF;
END $$;

-- Convenience view: closed trades with outcome label for ML retraining
CREATE OR REPLACE VIEW trade_log AS
SELECT
    id                                              AS signal_id,
    symbol,
    entry_date,
    entry_price,
    stop_loss                                       AS sl,
    target,
    strategies_fired,
    buy_probability                                 AS ai_prob_score,
    quality_score,
    exit_date,
    exit_price,
    exit_reason,
    pnl                                             AS pnl_inr,
    pnl_pct,
    held_days,
    regime,
    CASE WHEN status IN ('win') THEN 1 ELSE 0 END  AS hit_target
FROM trade_journal
WHERE status IN ('win', 'loss', 'stopped');

-- Strategy performance summary (weekly analytics)
CREATE OR REPLACE VIEW strategy_win_rates AS
SELECT
    unnested_strategy                        AS strategy,
    COUNT(*)                                 AS total_trades,
    SUM(CASE WHEN status = 'win' THEN 1 ELSE 0 END) AS wins,
    ROUND(
        100.0 * SUM(CASE WHEN status='win' THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0),
        1
    )                                        AS win_rate_pct,
    ROUND(AVG(CASE WHEN status='win' THEN pnl_pct END), 2) AS avg_win_pct,
    ROUND(AVG(CASE WHEN status IN ('loss','stopped') THEN pnl_pct END), 2) AS avg_loss_pct,
    MAX(entry_date)                          AS last_trade_date
FROM trade_journal
CROSS JOIN LATERAL unnest(string_to_array(strategy_tags, ',')) AS unnested_strategy
WHERE status IN ('win', 'loss', 'stopped')
  AND entry_date >= CURRENT_DATE - INTERVAL '90 days'
GROUP BY 1;
