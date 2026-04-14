"""
Portfolio Heat Checker — risk/portfolio_heat.py

Runs AFTER trade signals are generated to filter out signals that would
over-concentrate the portfolio.

Three rules:
  1. Sector cap (30% of capital per sector)
  2. Correlation block (flag if 60-day return correlation > 0.75 with any open position)
  3. Portfolio heat gate (total capital at risk > 6% of capital → suppress new signals)

Usage:
    from risk.portfolio_heat import PortfolioHeatChecker

    checker = PortfolioHeatChecker(account_capital=500_000)

    # Register open positions
    checker.add_position("RELIANCE", sector="Oil & Gas",
                         position_value_inr=45_000, risk_inr=1_500)

    # Check a new signal
    result = checker.check_signal(
        symbol="ONGC",
        sector="Oil & Gas",
        position_value_inr=30_000,
        risk_inr=900,
        price_series=ongc_close_series,   # pd.Series of 60+ closes
        open_price_series={"RELIANCE": reliance_close_series},
    )
    # {
    #   "allowed": False,
    #   "heat_pct": 4.8,
    #   "sector_exposure": {"Oil & Gas": 0.09, "IT": 0.12, ...},
    #   "correlated_with": ["RELIANCE"],
    #   "rejection_reason": "sector_limit",
    # }
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from strategies.sector_map import SECTOR_MAP

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

DEFAULT_SECTOR_CAP_PCT   = 30.0   # max % of capital in a single sector
DEFAULT_CORR_THRESHOLD   = 0.75   # flag if 60-day return correlation > this
DEFAULT_HEAT_CAP_PCT     = 6.0    # max total capital-at-risk across all positions
CORR_WINDOW              = 60     # trading days for correlation


# ── Position record ───────────────────────────────────────────────────────────

@dataclass
class OpenPosition:
    symbol:             str
    sector:             str
    position_value_inr: float
    risk_inr:           float           # max rupee risk for this trade
    entry_date:         Optional[str] = None


# ── Core checker ──────────────────────────────────────────────────────────────

class PortfolioHeatChecker:
    """
    Stateful checker: load open positions once, then call check_signal()
    for each new candidate.

    Args:
        account_capital:     Total trading capital in INR.
        sector_cap_pct:      Max % of capital allowed in any single sector (default 30%).
        corr_threshold:      60-day return correlation warn threshold (default 0.75).
        heat_cap_pct:        Max total risk-% across all open positions (default 6%).
    """

    def __init__(
        self,
        account_capital:   float = 100_000.0,
        sector_cap_pct:    float = DEFAULT_SECTOR_CAP_PCT,
        corr_threshold:    float = DEFAULT_CORR_THRESHOLD,
        heat_cap_pct:      float = DEFAULT_HEAT_CAP_PCT,
    ):
        self.account_capital  = account_capital
        self.sector_cap_pct   = sector_cap_pct
        self.corr_threshold   = corr_threshold
        self.heat_cap_pct     = heat_cap_pct
        self._positions: list[OpenPosition] = []

    # ── Position management ───────────────────────────────────────────────

    def add_position(
        self,
        symbol:             str,
        sector:             str | None = None,
        position_value_inr: float = 0.0,
        risk_inr:           float = 0.0,
        entry_date:         str | None = None,
    ) -> None:
        """Register an open position."""
        resolved_sector = sector or SECTOR_MAP.get(symbol.upper(), "Others")
        self._positions.append(OpenPosition(
            symbol             = symbol.upper(),
            sector             = resolved_sector,
            position_value_inr = position_value_inr,
            risk_inr           = risk_inr,
            entry_date         = entry_date,
        ))

    def clear_positions(self) -> None:
        self._positions.clear()

    @property
    def positions(self) -> list[OpenPosition]:
        return list(self._positions)

    # ── Derived metrics ───────────────────────────────────────────────────

    def sector_exposure(self) -> dict[str, float]:
        """
        Returns {sector: exposure_pct} where exposure_pct = sector_value / capital × 100.
        """
        totals: dict[str, float] = {}
        for p in self._positions:
            totals[p.sector] = totals.get(p.sector, 0.0) + p.position_value_inr
        return {
            sec: round(val / self.account_capital * 100, 2)
            for sec, val in totals.items()
        }

    def heat_pct(self) -> float:
        """Total capital at risk across all open positions as % of capital."""
        total_risk = sum(p.risk_inr for p in self._positions)
        return round(total_risk / self.account_capital * 100, 2)

    # ── Signal check ──────────────────────────────────────────────────────

    def check_signal(
        self,
        symbol:             str,
        sector:             str | None = None,
        position_value_inr: float = 0.0,
        risk_inr:           float = 0.0,
        price_series:       pd.Series | None = None,
        open_price_series:  dict[str, pd.Series] | None = None,
    ) -> dict:
        """
        Check whether adding this signal would violate any portfolio rule.

        Args:
            symbol:              New signal symbol.
            sector:              Sector override (auto-detected from SECTOR_MAP if None).
            position_value_inr:  Proposed position size in INR.
            risk_inr:            Proposed max risk in INR.
            price_series:        60-day close prices for the signal symbol (pd.Series).
            open_price_series:   {open_sym: pd.Series} close prices for open positions.
                                 Used for correlation calculation.

        Returns:
            {
              "allowed":          bool,
              "heat_pct":         float,
              "sector_exposure":  {sector: pct, ...},
              "correlated_with":  [sym, ...],   # empty if no high correlation
              "rejection_reason": str | None,   # "sector_limit" | "heat_limit" | None
            }
        """
        sym    = symbol.upper()
        sec    = sector or SECTOR_MAP.get(sym, "Others")
        exp    = self.sector_exposure()
        cur_heat = self.heat_pct()

        # ── Rule 3: Portfolio heat gate ───────────────────────────────────
        if cur_heat >= self.heat_cap_pct:
            return {
                "allowed":          False,
                "heat_pct":         cur_heat,
                "sector_exposure":  exp,
                "correlated_with":  [],
                "rejection_reason": "heat_limit",
            }

        # ── Rule 1: Sector cap ────────────────────────────────────────────
        new_sector_value = sum(
            p.position_value_inr for p in self._positions if p.sector == sec
        ) + position_value_inr
        new_sector_pct = new_sector_value / self.account_capital * 100

        if new_sector_pct > self.sector_cap_pct:
            return {
                "allowed":          False,
                "heat_pct":         cur_heat,
                "sector_exposure":  exp,
                "correlated_with":  [],
                "rejection_reason": "sector_limit",
            }

        # ── Rule 2: Correlation check (warning, not rejection) ────────────
        correlated_with: list[str] = []
        if price_series is not None and open_price_series:
            correlated_with = _find_correlated(
                sym, price_series, open_price_series,
                threshold=self.corr_threshold,
                window=CORR_WINDOW,
            )

        return {
            "allowed":          True,
            "heat_pct":         cur_heat,
            "sector_exposure":  exp,
            "correlated_with":  correlated_with,
            "rejection_reason": None,
        }

    # ── Bulk filter ───────────────────────────────────────────────────────

    def filter_signals(
        self,
        signals: list[dict],
        price_map: dict[str, pd.Series] | None = None,
    ) -> list[dict]:
        """
        Apply heat checks to a ranked list of trade signals (in-place augment).

        Each signal dict must have: symbol, position_size.position_value_inr,
        position_size.risk_inr.  sector is auto-detected.

        Each signal gets a "heat_check" key with the check_signal() result.
        Rejected signals are moved to the end but never removed (UI can grey them out).

        Args:
            signals:   Sorted list of result dicts from combined_signals.
            price_map: {symbol: pd.Series of closes} for correlation computation.
                       If None, correlation check is skipped.

        Returns:
            The same list, with "heat_check" added to each item.
        """
        open_series: dict[str, pd.Series] = {}

        for r in signals:
            sym = r.get("symbol", "").upper()
            sec = SECTOR_MAP.get(sym, "Others")
            ps  = r.get("position_size") or {}
            pos_val  = float(ps.get("position_value_inr") or 0)
            risk_val = float(ps.get("risk_inr")           or 0)

            price_s = (price_map or {}).get(sym)

            result = self.check_signal(
                symbol             = sym,
                sector             = sec,
                position_value_inr = pos_val,
                risk_inr           = risk_val,
                price_series       = price_s,
                open_price_series  = open_series if open_series else None,
            )
            r["heat_check"] = result

            # Add allowed signals to the virtual open position pool so later
            # signals in the same run are checked against them too.
            if result["allowed"] and pos_val > 0:
                self.add_position(sym, sector=sec,
                                  position_value_inr=pos_val, risk_inr=risk_val)
                if price_s is not None:
                    open_series[sym] = price_s

        return signals


# ── Correlation helper ────────────────────────────────────────────────────────

def _find_correlated(
    symbol:           str,
    price_series:     pd.Series,
    open_series:      dict[str, pd.Series],
    threshold:        float = DEFAULT_CORR_THRESHOLD,
    window:           int   = CORR_WINDOW,
) -> list[str]:
    """
    Return list of open-position symbols whose 60-day return correlation
    with `price_series` exceeds `threshold`.
    """
    correlated = []
    sig_rets = _returns(price_series, window)
    if sig_rets is None or len(sig_rets) < 10:
        return correlated

    for open_sym, open_s in open_series.items():
        open_rets = _returns(open_s, window)
        if open_rets is None or len(open_rets) < 10:
            continue
        try:
            # Align on common index
            combined = pd.DataFrame({"sig": sig_rets, "open": open_rets}).dropna()
            if len(combined) < 10:
                continue
            corr = combined["sig"].corr(combined["open"])
            if corr > threshold:
                correlated.append(open_sym)
        except Exception as e:
            logger.debug(f"Correlation calc error {symbol}/{open_sym}: {e}")

    return correlated


def _returns(series: pd.Series, window: int) -> pd.Series | None:
    """Compute daily pct returns for the last `window` periods."""
    try:
        s = series.iloc[-window:].astype(float)
        return s.pct_change().dropna()
    except Exception:
        return None
