-- Migration 004: Feature Snapshot
-- Stores a point-in-time snapshot of all ML inputs at trade creation.
-- Guarantees the feedback model trains on exact historical conditions,
-- not values re-derived from a potentially retrained model.

ALTER TABLE trade_journal
    ADD COLUMN IF NOT EXISTS feature_snapshot JSONB;

COMMENT ON COLUMN trade_journal.feature_snapshot IS
'Point-in-time ML snapshot captured at trade entry. Schema:
{
  "features":          { <26 FEATURE_NAMES: float> },
  "ml_prob":           float,       -- buy_probability at decision time
  "expected_return_pct": float,
  "regime":            "Bull" | "Neutral" | "Bear",
  "quality_score":     int,
  "score_breakdown":   { ml_probability, trend_strength, volume, sector, regime, multi_strategy },
  "strategies":        [str, ...],
  "strategy_count":    int,
  "captured_at":       ISO-8601 timestamp
}
NULL when the trade was logged before this migration.';

CREATE INDEX IF NOT EXISTS idx_journal_snapshot_not_null
    ON trade_journal ((feature_snapshot IS NOT NULL));
