"""
Unit tests for risk/liquidity_filter.py

Run with:
    python -m pytest tests/test_liquidity_filter.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import numpy as np
import pandas as pd

from risk.liquidity_filter import compute_liquidity, LiquidityFilter, ADV_WINDOW


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_df(
    n: int = 30,
    close: float = 500.0,
    volume: float = 500_000.0,
    high_pct: float = 0.2,    # high = close * (1 + high_pct/100)
    low_pct:  float = 0.2,    # low  = close * (1 - low_pct /100)
) -> pd.DataFrame:
    """Generate a minimal OHLCV dataframe."""
    closes  = np.full(n, close)
    volumes = np.full(n, volume)
    highs   = closes * (1 + high_pct / 100)
    lows    = closes * (1 - low_pct  / 100)
    opens   = closes
    return pd.DataFrame({
        "open":   opens,
        "high":   highs,
        "low":    lows,
        "close":  closes,
        "volume": volumes,
    })


def liquid(result):
    assert result["is_liquid"] is True,  f"Expected liquid, got: {result['rejection_reason']}"
    assert result["rejection_reason"] is None
    assert result["liquidity_score"] > 0


def illiquid(result):
    assert result["is_liquid"] is False, f"Expected illiquid, got liquid with score={result['liquidity_score']}"
    assert result["rejection_reason"] is not None
    assert result["liquidity_score"] == 0


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_none_df_returns_error(self):
        r = compute_liquidity(None, symbol="TEST")
        illiquid(r)
        assert "no price data" in r["rejection_reason"]

    def test_empty_df_returns_error(self):
        r = compute_liquidity(pd.DataFrame(), symbol="TEST")
        illiquid(r)

    def test_insufficient_rows(self):
        df = make_df(n=ADV_WINDOW - 1)
        r = compute_liquidity(df, symbol="TEST")
        illiquid(r)
        assert "insufficient history" in r["rejection_reason"]

    def test_missing_columns(self):
        df = make_df().drop(columns=["volume"])
        r = compute_liquidity(df, symbol="TEST")
        illiquid(r)
        assert "missing columns" in r["rejection_reason"]

    def test_exact_adv_window_rows_accepted(self):
        """Exactly ADV_WINDOW rows should pass the row-count check."""
        # ADV: 500_000 × ₹500 = ₹25 Cr/day; 0.4% daily range < 5% default threshold
        df = make_df(n=ADV_WINDOW, close=500.0, volume=500_000)
        r = compute_liquidity(df)
        liquid(r)


# ── ADV (turnover) filter ──────────────────────────────────────────────────────

class TestADV:

    def test_passes_above_min_adv(self):
        # 500_000 × ₹500 = ₹25 Cr/day; 0.4% daily range passes 5% default threshold
        df = make_df(close=500.0, volume=500_000)
        r = compute_liquidity(df)
        liquid(r)
        assert r["adv_inr_cr"] == pytest.approx(25.0, abs=1.0)

    def test_fails_below_min_adv(self):
        # 500 shares × ₹100 = ₹0.005 Cr/day — below ₹5 Cr
        df = make_df(close=100.0, volume=500)
        r = compute_liquidity(df)
        illiquid(r)
        assert "low liquidity" in r["rejection_reason"]

    def test_custom_min_adv(self):
        # 500 × 500 = ₹0.025 Cr/day — fails default ₹5 Cr, passes custom 0.01 Cr
        df = make_df(close=500.0, volume=500)
        r = compute_liquidity(df, min_adv_cr=0.01)
        liquid(r)

    def test_adv_shares_computed(self):
        df = make_df(close=500.0, volume=1_000_000)
        r = compute_liquidity(df)
        assert r["adv_shares"] == 1_000_000


# ── Spread filter ─────────────────────────────────────────────────────────────
# Spread proxy = 20-day average (high-low)/close.
# NSE large-caps: ~1-2%.  Default threshold: 5% (flags manipulated/illiquid stocks).

class TestSpread:

    def test_narrow_spread_passes(self):
        # 1.5% daily range → well below 5% default threshold
        df = make_df(close=500.0, volume=500_000, high_pct=0.75, low_pct=0.75)
        r = compute_liquidity(df)
        liquid(r)
        assert r["spread_pct"] == pytest.approx(1.5, abs=0.1)

    def test_wide_spread_fails(self):
        # 8% daily range (±4%) → fails default 5% threshold
        df = make_df(close=500.0, volume=500_000, high_pct=4.0, low_pct=4.0)
        r = compute_liquidity(df)
        illiquid(r)
        assert "wide spread" in r["rejection_reason"]

    def test_custom_spread_threshold(self):
        # 3% daily range — fails tight 2.5% threshold, passes relaxed 4% threshold
        df = make_df(close=500.0, volume=500_000, high_pct=1.5, low_pct=1.5)
        r_tight = compute_liquidity(df, max_spread_pct=2.5)
        illiquid(r_tight)
        r_relax = compute_liquidity(df, max_spread_pct=4.0)
        liquid(r_relax)


# ── Promoter holding filter ───────────────────────────────────────────────────

class TestPromoterHolding:

    def _make_shareholding_patch(self, symbol, pct, monkeypatch):
        """Patch the module-level cache so no file I/O is needed."""
        import risk.liquidity_filter as lf_module
        monkeypatch.setattr(lf_module, "_shareholding_cache", {symbol: pct})

    def test_low_promoter_passes(self, monkeypatch):
        df = make_df(close=500.0, volume=500_000, high_pct=0.2, low_pct=0.2)
        self._make_shareholding_patch("GOOD", 40.0, monkeypatch)
        r = compute_liquidity(df, symbol="GOOD")
        liquid(r)
        assert r["promoter_holding_pct"] == 40.0

    def test_high_promoter_fails(self, monkeypatch):
        df = make_df(close=500.0, volume=500_000, high_pct=0.2, low_pct=0.2)
        self._make_shareholding_patch("BAD", 80.0, monkeypatch)
        r = compute_liquidity(df, symbol="BAD")
        illiquid(r)
        assert "low float" in r["rejection_reason"]

    def test_missing_promoter_data_neutral(self, monkeypatch):
        """Unknown promoter holding should NOT block — treated as neutral."""
        import risk.liquidity_filter as lf_module
        monkeypatch.setattr(lf_module, "_shareholding_cache", {})
        df = make_df(close=500.0, volume=500_000, high_pct=0.2, low_pct=0.2)
        r = compute_liquidity(df, symbol="UNKNOWN")
        liquid(r)
        assert r["promoter_holding_pct"] is None


# ── Score and output structure ─────────────────────────────────────────────────

class TestOutputStructure:

    REQUIRED_KEYS = {
        "liquidity_score", "is_liquid", "adv_shares", "adv_inr_cr",
        "spread_pct", "promoter_holding_pct", "rejection_reason",
    }

    def test_all_keys_present_liquid(self):
        df = make_df(close=500.0, volume=500_000, high_pct=0.2, low_pct=0.2)
        r = compute_liquidity(df)
        assert self.REQUIRED_KEYS == set(r.keys())

    def test_all_keys_present_illiquid(self):
        r = compute_liquidity(None)
        assert self.REQUIRED_KEYS == set(r.keys())

    def test_score_in_range(self):
        df = make_df(close=500.0, volume=500_000, high_pct=0.2, low_pct=0.2)
        r = compute_liquidity(df)
        assert 0 <= r["liquidity_score"] <= 100

    def test_high_adv_gives_high_score(self):
        # Very high ADV (50× minimum) → high score
        df = make_df(close=1000.0, volume=10_000_000, high_pct=0.1, low_pct=0.1)
        r = compute_liquidity(df)
        liquid(r)
        assert r["liquidity_score"] >= 70


# ── LiquidityFilter class ─────────────────────────────────────────────────────

class TestLiquidityFilterClass:

    def test_class_returns_same_as_function(self):
        df = make_df(close=500.0, volume=500_000, high_pct=0.2, low_pct=0.2)
        direct  = compute_liquidity(df, symbol="X", close_price=500.0)
        via_cls = LiquidityFilter().check(df, symbol="X", close_price=500.0)
        assert direct == via_cls

    def test_custom_threshold_on_class(self):
        # 500 × 200 = ₹1 Cr/day — fails default ₹5 Cr but passes ₹0.5 Cr threshold
        df = make_df(close=200.0, volume=500, high_pct=0.2, low_pct=0.2)
        lf = LiquidityFilter(min_adv_cr=0.001)
        r  = lf.check(df)
        liquid(r)
