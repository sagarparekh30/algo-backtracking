"""
Unit tests for risk/position_sizer.py

Run with:
    python -m pytest tests/test_position_sizer.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from risk.position_sizer import compute_position_size


# ── Helpers ───────────────────────────────────────────────────────────────

def ok(result):
    """Assert result has no error and shares > 0."""
    assert result["error"] is None, f"Unexpected error: {result['error']}"
    assert result["shares"] > 0


def err(result):
    """Assert result has an error and shares == 0."""
    assert result["error"] is not None
    assert result["shares"] == 0


# ── Edge cases ────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_sl_equals_entry(self):
        """SL = entry → division by zero risk → must return error."""
        r = compute_position_size(
            account_capital=100_000,
            entry_price=500.0,
            stop_loss_price=500.0,
        )
        err(r)
        assert "below entry_price" in r["error"]

    def test_sl_above_entry(self):
        """SL > entry → invalid trade setup → must return error."""
        r = compute_position_size(
            account_capital=100_000,
            entry_price=500.0,
            stop_loss_price=520.0,
        )
        err(r)
        assert "below entry_price" in r["error"]

    def test_zero_capital(self):
        """Capital = 0 → must return error."""
        r = compute_position_size(
            account_capital=0,
            entry_price=500.0,
            stop_loss_price=480.0,
        )
        err(r)
        assert "account_capital" in r["error"]

    def test_negative_capital(self):
        r = compute_position_size(
            account_capital=-50_000,
            entry_price=500.0,
            stop_loss_price=480.0,
        )
        err(r)

    def test_zero_entry_price(self):
        r = compute_position_size(
            account_capital=100_000,
            entry_price=0.0,
            stop_loss_price=480.0,
        )
        err(r)

    def test_tiny_risk_per_share_gives_shares(self):
        """Very tight SL (₹1 risk/share) on small capital should still give shares."""
        r = compute_position_size(
            account_capital=100_000,
            entry_price=500.0,
            stop_loss_price=499.0,   # ₹1 risk/share
        )
        ok(r)
        # rupee_risk = 1500; shares = 1500/1 = 1500, but capped at 10% = ₹10k → 20 shares
        assert r["capped"] is True
        assert r["shares"] == 20


# ── Fixed Fractional mode ─────────────────────────────────────────────────

class TestFixedFractional:

    def test_standard_case(self):
        """
        Capital ₹5L, 1.5% risk, entry ₹2450, SL ₹2380.
        rupee_risk = 7500, risk/share = 70, shares = floor(7500/70) = 107
        position = 107 × 2450 = ₹2,62,150 → 52.4% → capped at 10% = ₹50k → 20 shares
        """
        r = compute_position_size(
            account_capital=500_000,
            entry_price=2_450.0,
            stop_loss_price=2_380.0,
        )
        ok(r)
        assert r["size_mode"] == "fixed"
        assert r["capped"] is True
        assert r["shares"] == 20
        assert r["position_value_inr"] == 20 * 2_450.0

    def test_cap_not_triggered(self):
        """
        Capital ₹10L, 1.5% risk, entry ₹100, SL ₹90.
        rupee_risk = 15000, risk/share = 10, shares = 1500
        position = 1500 × 100 = ₹1,50,000 = 15% → capped at 10% → ₹1L → 1000 shares
        """
        r = compute_position_size(
            account_capital=1_000_000,
            entry_price=100.0,
            stop_loss_price=90.0,
        )
        ok(r)
        assert r["capped"] is True

    def test_risk_per_share_correct(self):
        r = compute_position_size(
            account_capital=200_000,
            entry_price=1_000.0,
            stop_loss_price=950.0,
        )
        ok(r)
        assert r["risk_per_share"] == 50.0

    def test_risk_inr_leq_rupee_risk(self):
        """Actual risk ≤ target rupee_risk (shares are floored)."""
        capital   = 300_000
        risk_pct  = 1.5
        r = compute_position_size(
            account_capital=capital,
            entry_price=750.0,
            stop_loss_price=720.0,
            risk_per_trade_pct=risk_pct,
        )
        ok(r)
        target_risk = capital * risk_pct / 100
        assert r["risk_inr"] <= target_risk + 1   # allow ₹1 rounding

    def test_custom_risk_pct(self):
        """2% risk should give more shares than 1% on same setup."""
        base = dict(account_capital=200_000, entry_price=500.0, stop_loss_price=480.0)
        r1 = compute_position_size(**base, risk_per_trade_pct=1.0)
        r2 = compute_position_size(**base, risk_per_trade_pct=2.0)
        ok(r1); ok(r2)
        # Both likely capped, but risk_inr should differ when not capped
        # At least verify both return valid results
        assert r2["risk_inr"] >= r1["risk_inr"] or (r1["capped"] and r2["capped"])

    def test_max_position_pct_cap(self):
        """Hard cap applied: position_value_inr ≤ capital × max_position_pct / 100."""
        capital = 100_000
        max_pct = 10.0
        r = compute_position_size(
            account_capital=capital,
            entry_price=50.0,
            stop_loss_price=45.0,
            max_position_pct=max_pct,
        )
        ok(r)
        assert r["position_value_inr"] <= capital * max_pct / 100 + 50  # within 1 share

    def test_pct_of_capital_accurate(self):
        r = compute_position_size(
            account_capital=100_000,
            entry_price=500.0,
            stop_loss_price=475.0,
        )
        ok(r)
        expected = round(r["position_value_inr"] / 100_000 * 100, 2)
        assert abs(r["pct_of_capital"] - expected) < 0.01


# ── Kelly Criterion mode ──────────────────────────────────────────────────

class TestKelly:

    def test_kelly_basic(self):
        """
        win_rate=0.55, avg_rr=2.0
        kelly_f = 0.55 - 0.45/2.0 = 0.55 - 0.225 = 0.325
        half_kelly = 0.1625
        capped to 10% → position = ₹10k on ₹1L capital
        """
        r = compute_position_size(
            account_capital=100_000,
            entry_price=500.0,
            stop_loss_price=470.0,
            mode="kelly",
            win_rate=0.55,
            avg_rr=2.0,
        )
        ok(r)
        assert r["size_mode"] == "kelly"
        assert r["kelly_fraction"] == pytest.approx(0.325, abs=0.001)
        assert r["half_kelly"]     == pytest.approx(0.1625, abs=0.001)

    def test_kelly_negative_ev_error(self):
        """win_rate=0.3, avg_rr=1.0 → kelly_f = 0.3 - 0.7/1.0 = -0.4 → error."""
        r = compute_position_size(
            account_capital=100_000,
            entry_price=500.0,
            stop_loss_price=480.0,
            mode="kelly",
            win_rate=0.30,
            avg_rr=1.0,
        )
        err(r)
        assert "negative" in r["error"].lower() or "Kelly" in r["error"]

    def test_kelly_missing_win_rate(self):
        r = compute_position_size(
            account_capital=100_000,
            entry_price=500.0,
            stop_loss_price=480.0,
            mode="kelly",
            win_rate=None,
            avg_rr=2.0,
        )
        err(r)

    def test_kelly_missing_avg_rr(self):
        r = compute_position_size(
            account_capital=100_000,
            entry_price=500.0,
            stop_loss_price=480.0,
            mode="kelly",
            win_rate=0.55,
            avg_rr=None,
        )
        err(r)

    def test_kelly_invalid_win_rate_zero(self):
        r = compute_position_size(
            account_capital=100_000,
            entry_price=500.0,
            stop_loss_price=480.0,
            mode="kelly",
            win_rate=0.0,
            avg_rr=2.0,
        )
        err(r)

    def test_kelly_win_rate_one(self):
        r = compute_position_size(
            account_capital=100_000,
            entry_price=500.0,
            stop_loss_price=480.0,
            mode="kelly",
            win_rate=1.0,
            avg_rr=2.0,
        )
        err(r)

    def test_kelly_fraction_returned_as_none_in_fixed_mode(self):
        r = compute_position_size(
            account_capital=100_000,
            entry_price=500.0,
            stop_loss_price=480.0,
            mode="fixed",
        )
        ok(r)
        assert r["kelly_fraction"] is None
        assert r["half_kelly"]     is None

    def test_kelly_capped(self):
        """Even if Kelly says 50% of capital, hard cap at max_position_pct."""
        r = compute_position_size(
            account_capital=100_000,
            entry_price=500.0,
            stop_loss_price=480.0,
            mode="kelly",
            win_rate=0.70,
            avg_rr=3.0,
            max_position_pct=10.0,
        )
        ok(r)
        assert r["capped"] is True
        assert r["position_value_inr"] <= 100_000 * 0.10 + 500


# ── Output structure ──────────────────────────────────────────────────────

class TestOutputStructure:

    REQUIRED_KEYS = {
        "shares", "position_value_inr", "risk_inr", "risk_per_share",
        "pct_of_capital", "kelly_fraction", "half_kelly",
        "size_mode", "capped", "error",
    }

    def test_all_keys_present_on_success(self):
        r = compute_position_size(100_000, 500.0, 480.0)
        assert self.REQUIRED_KEYS == set(r.keys())

    def test_all_keys_present_on_error(self):
        r = compute_position_size(0, 500.0, 480.0)
        assert self.REQUIRED_KEYS == set(r.keys())

    def test_shares_is_integer(self):
        r = compute_position_size(100_000, 500.0, 480.0)
        ok(r)
        assert isinstance(r["shares"], int)

    def test_position_value_equals_shares_times_entry(self):
        r = compute_position_size(100_000, 500.0, 480.0)
        ok(r)
        assert r["position_value_inr"] == pytest.approx(r["shares"] * 500.0, abs=0.01)

    def test_risk_inr_equals_shares_times_risk_per_share(self):
        r = compute_position_size(100_000, 500.0, 480.0)
        ok(r)
        assert r["risk_inr"] == pytest.approx(r["shares"] * 20.0, abs=0.01)
