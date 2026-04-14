-- Migration 008: stock_metadata table
-- Stores per-symbol metadata: sector, industry, index membership, company name, ISIN.
-- Populated by fetcher/stock_metadata_fetcher.py
-- Queried by screener filters and sector heatmap.

DO $$ BEGIN

  -- ── Main metadata table ──────────────────────────────────────────────────
  IF NOT EXISTS (
    SELECT FROM information_schema.tables WHERE table_name = 'stock_metadata'
  ) THEN
    CREATE TABLE stock_metadata (
      symbol          TEXT        PRIMARY KEY,
      company_name    TEXT,
      sector          TEXT,                       -- e.g. "IT", "Banking", "Pharma"
      industry        TEXT,                       -- finer grouping from yfinance
      indices         TEXT[]      DEFAULT '{}',   -- ["NIFTY 50","NIFTY IT",...]
      market_cap_cat  TEXT,                       -- "Large Cap"|"Mid Cap"|"Small Cap"|"Micro Cap"
      isin            TEXT,
      face_value      NUMERIC,
      series          TEXT        DEFAULT 'EQ',
      is_active       BOOLEAN     DEFAULT TRUE,
      updated_at      TIMESTAMPTZ DEFAULT NOW()
    );
    RAISE NOTICE 'Created stock_metadata table';
  END IF;

  -- ── Indices ──────────────────────────────────────────────────────────────
  IF NOT EXISTS (
    SELECT FROM pg_indexes WHERE tablename = 'stock_metadata' AND indexname = 'idx_smeta_sector'
  ) THEN
    CREATE INDEX idx_smeta_sector  ON stock_metadata (sector);
    CREATE INDEX idx_smeta_indices ON stock_metadata USING GIN (indices);
    CREATE INDEX idx_smeta_mcap    ON stock_metadata (market_cap_cat);
  END IF;

END $$;
