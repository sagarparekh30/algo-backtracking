"""
Portfolio Builder

Given a capital amount and a list of ranked trade signals, builds an
optimal portfolio by:

  1. Selecting top N trades (by quality score)
  2. Sizing each position using ATR-based risk (1-2% per trade)
  3. Enforcing sector diversification (max 2 stocks per sector)
  4. Respecting max capital allocation per position
  5. Returning full allocation plan with P&L targets

Usage:
    from risk.portfolio_builder import build_portfolio

    result = build_portfolio(
        capital=100000,
        trades=[...],         # from /api/combined/signals
        risk_pct=1.5,
        max_positions=8,
        max_sector_exposure=2,
    )
"""

from __future__ import annotations
from typing import Optional
from strategies.sector_map import get_sector


def build_portfolio(
    capital: float,
    trades: list,
    risk_pct: float = 1.5,
    max_positions: int = 8,
    max_sector_exposure: int = 2,
    max_position_pct: float = 20.0,
) -> dict:
    """
    Build an allocated portfolio from ranked trade signals.

    Args:
        capital:             Total trading capital (INR).
        trades:              List of trade dicts from /api/combined/signals,
                             sorted by quality_score desc.
        risk_pct:            % of capital to risk per trade (default 1.5%).
        max_positions:       Maximum number of simultaneous positions.
        max_sector_exposure: Max stocks from the same sector (default 2).
        max_position_pct:    Max % of capital in any single position (default 20%).

    Returns:
        {
          "positions": [...],
          "summary": {
            "total_deployed", "total_at_risk", "cash_remaining",
            "expected_return", "positions_count", "sectors_used"
          }
        }
    """
    if not trades or capital <= 0:
        return {"positions": [], "summary": _empty_summary(capital)}

    risk_amount_per_trade = capital * (risk_pct / 100.0)
    max_position_value    = capital * (max_position_pct / 100.0)

    sector_count: dict[str, int] = {}
    positions = []
    deployed  = 0.0

    # Trades are expected sorted by quality_score desc — take that order
    for trade in trades:
        if len(positions) >= max_positions:
            break

        sym    = trade.get("symbol", "")
        entry  = float(trade.get("entry") or trade.get("price") or 0)
        sl     = float(trade.get("stop_loss") or 0)
        target = float(trade.get("target") or trade.get("price_target") or 0)

        if entry <= 0 or sl <= 0 or sl >= entry:
            continue

        # Sector diversification check
        sector = get_sector(sym)
        if sector_count.get(sector, 0) >= max_sector_exposure:
            continue

        # Position sizing: risk ₹ / risk per share
        risk_per_share = entry - sl
        if risk_per_share <= 0:
            continue

        shares = int(risk_amount_per_trade / risk_per_share)
        if shares <= 0:
            continue

        position_value = shares * entry

        # Cap position to max_position_pct of capital
        if position_value > max_position_value:
            shares         = int(max_position_value / entry)
            position_value = shares * entry

        # Cap to remaining capital
        if deployed + position_value > capital:
            shares         = int((capital - deployed) / entry)
            position_value = shares * entry
            if shares <= 0:
                break

        # Calculate metrics
        stop_value    = shares * sl
        target_value  = shares * target if target > 0 else None
        risk_in_trade = shares * risk_per_share
        reward        = (shares * (target - entry)) if target > 0 else None
        rr_ratio      = round(reward / risk_in_trade, 2) if reward and risk_in_trade > 0 else None

        pct_of_portfolio = round(position_value / capital * 100, 1)

        positions.append({
            "symbol":             sym,
            "sector":             sector,
            "entry":              round(entry, 2),
            "stop_loss":          round(sl, 2),
            "target":             round(target, 2) if target > 0 else None,
            "shares":             shares,
            "position_value":     round(position_value, 2),
            "pct_of_portfolio":   pct_of_portfolio,
            "risk_amount":        round(risk_in_trade, 2),
            "potential_reward":   round(reward, 2) if reward else None,
            "rr_ratio":           rr_ratio,
            "quality_score":      trade.get("quality_score"),
            "confidence":         trade.get("confidence"),
            "buy_probability":    trade.get("buy_probability"),
            "strategies":         trade.get("strategies", []),
            "strategy_count":     trade.get("strategy_count", 1),
        })

        sector_count[sector] = sector_count.get(sector, 0) + 1
        deployed += position_value

    # Summary
    total_risk   = sum(p["risk_amount"]      for p in positions)
    total_reward = sum(p["potential_reward"] for p in positions if p["potential_reward"])
    cash         = capital - deployed

    summary = {
        "capital":           round(capital, 2),
        "total_deployed":    round(deployed, 2),
        "deployed_pct":      round(deployed / capital * 100, 1) if capital > 0 else 0,
        "cash_remaining":    round(cash, 2),
        "total_at_risk":     round(total_risk, 2),
        "risk_pct_of_cap":   round(total_risk / capital * 100, 2) if capital > 0 else 0,
        "expected_reward":   round(total_reward, 2),
        "positions_count":   len(positions),
        "sectors_used":      list(sector_count.keys()),
        "risk_per_trade_pct": risk_pct,
        "max_positions":     max_positions,
    }

    return {"positions": positions, "summary": summary}


def _empty_summary(capital: float) -> dict:
    return {
        "capital": capital, "total_deployed": 0, "deployed_pct": 0,
        "cash_remaining": capital, "total_at_risk": 0, "risk_pct_of_cap": 0,
        "expected_reward": 0, "positions_count": 0, "sectors_used": [],
        "risk_per_trade_pct": 0, "max_positions": 0,
    }
