"""
Backtesting engine — simulates strategy signals on historical data
without look-ahead bias.

Entry:  signal generated on bar N → entry at bar N+1 open price
Exits:
  - Stop loss: bar low <= stop_loss price
  - Target:    bar high >= target price
  - Time:      max hold days exceeded, exit at close
"""

import os
import sys
import logging
import math
import time
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import TABLE_NAME, INITIAL_CAPITAL, MAX_HOLD_DAYS
from db.connection import get_engine
from risk.manager import RiskManager

logger = logging.getLogger(__name__)

# Minimum history required before generating signals
MIN_HISTORY = 200


class BacktestEngine:
    """
    Event-driven backtesting engine that simulates one trade per symbol
    at a time (no simultaneous positions per symbol).
    """

    def __init__(
        self,
        db_path: str = None,
        table_name: str = None,
        risk_manager: RiskManager = None,
    ):
        self.table_name = table_name or TABLE_NAME
        self.risk_manager = risk_manager or RiskManager()
        self.initial_capital = self.risk_manager.capital

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        strategy_id: str,
        symbol: str = None,
        start_date: str = None,
        end_date: str = None,
    ) -> dict:
        """
        Run a backtest for a strategy.

        Args:
            strategy_id: Strategy key (e.g. 'golden_rsi').
            symbol: If provided, only backtest this symbol; else all symbols.
            start_date: ISO date string — only count trades on or after this date.
            end_date: ISO date string — only count trades on or before this date.

        Returns:
            dict with keys: trades, metrics, equity_curve, per_symbol
        """
        _t0 = time.time()
        all_trades = []
        per_symbol = {}

        # Resolve strategy function once — avoids re-instantiating StrategyManager per bar
        from strategies.swing_executor import StrategyManager
        strategy_fn = StrategyManager()._get_strategy_fn(strategy_id)
        if strategy_fn is None:
            return {
                "trades": [],
                "metrics": self._calculate_metrics([], self.initial_capital),
                "equity_curve": self._build_equity_curve([], self.initial_capital),
                "per_symbol": {},
                "error": f"Unknown strategy: {strategy_id}",
            }

        # Parse the requested trade window (for post-simulation filtering)
        trade_start = pd.to_datetime(start_date) if start_date else None
        trade_end   = pd.to_datetime(end_date)   if end_date   else None

        # Extend the data load window backwards so indicators are fully warmed
        # up before the first bar the user asked about.
        # MIN_HISTORY=200 trading days ≈ 280 calendar days.  We add an extra
        # 60-day buffer to handle weekends/holidays, giving ~340 days total.
        # This is fixed regardless of how short the requested trade window is —
        # so even a 1W or 15D backtest gets correct RSI/EMA/MACD values.
        WARMUP_CALENDAR_DAYS = int(MIN_HISTORY * 1.7)   # ~340 days
        if trade_start is not None:
            data_start = (trade_start - timedelta(days=WARMUP_CALENDAR_DAYS)).strftime("%Y-%m-%d")
        else:
            data_start = None

        try:
            conn = get_engine()

            if symbol:
                symbols = [symbol]
            else:
                symbols = pd.read_sql(
                    f"SELECT DISTINCT symbol FROM {self.table_name}", conn
                )["symbol"].tolist()

            for sym in symbols:
                try:
                    # Load with warm-up buffer; end_date stays as requested
                    df = self._load_symbol_data(sym, data_start, end_date)
                    if df is None or len(df) < MIN_HISTORY + 5:
                        continue

                    trades = self._simulate_symbol(df, sym, strategy_fn)

                    # Filter trades to the user-requested date window
                    if trade_start is not None:
                        trades = [t for t in trades if pd.to_datetime(t["entry_date"]) >= trade_start]
                    if trade_end is not None:
                        trades = [t for t in trades if pd.to_datetime(t["entry_date"]) <= trade_end]

                    if trades:
                        all_trades.extend(trades)
                        per_symbol[sym] = self._calculate_metrics(
                            trades, self.initial_capital
                        )
                except Exception as e:
                    logger.error(f"Backtest error for {sym}: {e}")

        except Exception as e:
            logger.error(f"BacktestEngine.run error: {e}")

        metrics = self._calculate_metrics(all_trades, self.initial_capital)
        equity_curve = self._build_equity_curve(all_trades, self.initial_capital)
        elapsed_ms = int((time.time() - _t0) * 1000)

        result = {
            "trades": all_trades,
            "metrics": metrics,
            "equity_curve": equity_curve,
            "per_symbol": per_symbol,
        }

        # Persist run to DB (non-blocking — failure does not affect the result)
        try:
            run_id = self._persist_run(
                strategy_id=strategy_id,
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                metrics=metrics,
                trades=all_trades,
                equity_curve=equity_curve,
                per_symbol=per_symbol,
                elapsed_ms=elapsed_ms,
            )
            result["run_id"] = run_id
        except Exception as e:
            logger.warning(f"Could not persist backtest run: {e}")

        return result

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_run(
        self,
        strategy_id: str,
        symbol: str | None,
        start_date: str | None,
        end_date: str | None,
        metrics: dict,
        trades: list,
        equity_curve: list,
        per_symbol: dict,
        elapsed_ms: int = 0,
    ) -> int | None:
        """
        Save a completed backtest run to the DB.
        Returns the run_id on success, None on failure.
        """
        engine = get_engine()
        with engine.begin() as conn:
            # 1. Insert run summary
            row = conn.execute(
                """
                INSERT INTO backtest_runs (
                    strategy, symbol, start_date, end_date,
                    capital, risk_pct,
                    total_trades, win_count, loss_count, win_rate,
                    total_pnl, total_pnl_pct, avg_pnl_trade,
                    max_drawdown, max_drawdown_pct,
                    sharpe_ratio, profit_factor,
                    best_trade, worst_trade, avg_hold_days,
                    run_duration_ms
                ) VALUES (
                    %(strategy)s, %(symbol)s, %(start_date)s, %(end_date)s,
                    %(capital)s, %(risk_pct)s,
                    %(total_trades)s, %(win_count)s, %(loss_count)s, %(win_rate)s,
                    %(total_pnl)s, %(total_pnl_pct)s, %(avg_pnl_trade)s,
                    %(max_drawdown)s, %(max_drawdown_pct)s,
                    %(sharpe_ratio)s, %(profit_factor)s,
                    %(best_trade)s, %(worst_trade)s, %(avg_hold_days)s,
                    %(run_duration_ms)s
                )
                RETURNING id
                """,
                {
                    "strategy":        strategy_id,
                    "symbol":          symbol,
                    "start_date":      start_date,
                    "end_date":        end_date,
                    "capital":         self.initial_capital,
                    "risk_pct":        self.risk_manager.risk_pct,
                    "total_trades":    metrics.get("total_trades", 0),
                    "win_count":       metrics.get("win_count", 0),
                    "loss_count":      metrics.get("loss_count", 0),
                    "win_rate":        metrics.get("win_rate"),
                    "total_pnl":       metrics.get("total_pnl"),
                    "total_pnl_pct":   metrics.get("total_pnl_pct"),
                    "avg_pnl_trade":   metrics.get("avg_pnl_per_trade"),
                    "max_drawdown":    metrics.get("max_drawdown"),
                    "max_drawdown_pct":metrics.get("max_drawdown_pct"),
                    "sharpe_ratio":    metrics.get("sharpe_ratio"),
                    "profit_factor":   metrics.get("profit_factor"),
                    "best_trade":      metrics.get("best_trade"),
                    "worst_trade":     metrics.get("worst_trade"),
                    "avg_hold_days":   metrics.get("avg_hold_days"),
                    "run_duration_ms": elapsed_ms,
                },
            )
            run_id = row.fetchone()[0]

            # 2. Insert individual trades
            if trades:
                conn.execute(
                    """
                    INSERT INTO backtest_trades (
                        run_id, symbol, entry_date, exit_date,
                        entry_price, exit_price, stop_loss, target,
                        shares, pnl, pnl_pct, exit_reason, hold_days
                    ) VALUES (
                        %(run_id)s, %(symbol)s, %(entry_date)s, %(exit_date)s,
                        %(entry_price)s, %(exit_price)s, %(stop_loss)s, %(target)s,
                        %(shares)s, %(pnl)s, %(pnl_pct)s, %(exit_reason)s, %(hold_days)s
                    )
                    """,
                    [{"run_id": run_id, **t} for t in trades],
                )

            # 3. Insert equity curve
            if equity_curve:
                conn.execute(
                    """
                    INSERT INTO backtest_equity_curve (run_id, curve_date, equity)
                    VALUES (%(run_id)s, %(date)s, %(equity)s)
                    ON CONFLICT (run_id, curve_date) DO UPDATE SET equity = EXCLUDED.equity
                    """,
                    [{"run_id": run_id, "date": p["date"], "equity": p["equity"]}
                     for p in equity_curve],
                )

            # 4. Insert per-symbol summary
            if per_symbol:
                conn.execute(
                    """
                    INSERT INTO backtest_per_symbol (
                        run_id, symbol, total_trades, win_count, loss_count,
                        win_rate, total_pnl, total_pnl_pct,
                        sharpe_ratio, profit_factor, max_drawdown
                    ) VALUES (
                        %(run_id)s, %(symbol)s, %(total_trades)s, %(win_count)s, %(loss_count)s,
                        %(win_rate)s, %(total_pnl)s, %(total_pnl_pct)s,
                        %(sharpe_ratio)s, %(profit_factor)s, %(max_drawdown)s
                    )
                    ON CONFLICT (run_id, symbol) DO NOTHING
                    """,
                    [
                        {
                            "run_id":       run_id,
                            "symbol":       sym,
                            "total_trades": m.get("total_trades"),
                            "win_count":    m.get("win_count"),
                            "loss_count":   m.get("loss_count"),
                            "win_rate":     m.get("win_rate"),
                            "total_pnl":    m.get("total_pnl"),
                            "total_pnl_pct":m.get("total_pnl_pct"),
                            "sharpe_ratio": m.get("sharpe_ratio"),
                            "profit_factor":m.get("profit_factor"),
                            "max_drawdown": m.get("max_drawdown"),
                        }
                        for sym, m in per_symbol.items()
                    ],
                )

        logger.info(f"Backtest run {run_id} persisted ({len(trades)} trades, {elapsed_ms}ms)")
        return run_id

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_symbol_data(
        self,
        symbol: str,
        start_date: str = None,
        end_date: str = None,
    ) -> pd.DataFrame | None:
        """
        Load OHLCV data for a symbol from the database.

        start_date / end_date bound the rows fetched from the DB.
        For backtesting with a user date range, callers should pass a
        start_date that includes the warm-up buffer (see run()).
        """
        try:
            engine = get_engine()

            # Build query with optional date bounds pushed to SQL for efficiency
            conditions = ["symbol = %(sym)s"]
            params: dict = {"sym": symbol}
            if start_date:
                conditions.append("trade_date >= %(start)s")
                params["start"] = start_date
            if end_date:
                conditions.append("trade_date <= %(end)s")
                params["end"] = end_date

            where = " AND ".join(conditions)
            query = (
                f"SELECT trade_date, open, high, low, close, volume "
                f"FROM {self.table_name} WHERE {where} ORDER BY trade_date ASC"
            )
            df = pd.read_sql(query, engine, params=params)

            if df.empty:
                return None

            df["trade_date"] = pd.to_datetime(df["trade_date"])
            df = df.reset_index(drop=True)
            return df

        except Exception as e:
            logger.error(f"_load_symbol_data error for {symbol}: {e}")
            return None

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------

    def _simulate_symbol(
        self, df: pd.DataFrame, symbol: str, strategy_fn
    ) -> list:
        """
        Simulate trading on a single symbol's price history.

        No look-ahead bias:
          - Signal generated at bar i using df.iloc[:i+1]
          - Entry is at bar i+1 open price

        Returns:
            List of trade dicts.
        """
        trades = []
        in_trade = False
        entry_price = 0.0
        stop_loss = 0.0
        target = 0.0
        shares = 0
        entry_date = None
        entry_idx = 0
        max_hold = MAX_HOLD_DAYS
        running_capital = self.initial_capital  # tracks equity for realistic sizing

        n = len(df)

        for i in range(MIN_HISTORY, n - 1):
            # ---- If not in a trade, check for a new signal ----
            if not in_trade:
                df_window = df.iloc[: i + 1].copy()
                signal = self._get_strategy_signal(df_window, symbol, strategy_fn)

                if signal is not None:
                    # Entry at next bar's open
                    next_bar = df.iloc[i + 1]
                    entry_price = float(next_bar["open"])

                    atr_val = signal.get("atr", 0.0) or 0.0
                    trade_params = self.risk_manager.get_trade_params(
                        entry_price, atr_val, available_capital=running_capital
                    )

                    stop_loss = trade_params["stop_loss"]
                    target = trade_params["target"]
                    shares = trade_params["shares"]

                    if shares <= 0 or stop_loss >= entry_price:
                        continue  # Skip invalid trade

                    entry_date = next_bar["trade_date"]
                    entry_idx = i + 1
                    in_trade = True

            # ---- If in a trade, check for exit ----
            elif in_trade:
                current_bar = df.iloc[i]
                bars_held = i - entry_idx

                exit_price = None
                exit_reason = None

                bar_low  = float(current_bar["low"])
                bar_high = float(current_bar["high"])
                hit_stop   = bar_low  <= stop_loss
                hit_target = bar_high >= target

                # Both hit on the same bar — conservative assumption: stop wins
                # (open gap could have blown through both levels; we can't know
                #  which happened first from daily OHLC alone)
                if hit_stop and hit_target:
                    exit_price  = stop_loss
                    exit_reason = "Stop Loss (conflict)"
                elif hit_stop:
                    exit_price  = stop_loss
                    exit_reason = "Stop Loss"
                elif hit_target:
                    exit_price  = target
                    exit_reason = "Target Hit"

                # Check max hold days
                elif bars_held >= max_hold:
                    exit_price = float(current_bar["close"])
                    exit_reason = "Max Hold Days"

                if exit_price is not None:
                    pnl = (exit_price - entry_price) * shares
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0

                    trades.append(
                        {
                            "symbol": symbol,
                            "entry_date": entry_date.strftime("%Y-%m-%d")
                            if hasattr(entry_date, "strftime")
                            else str(entry_date),
                            "exit_date": current_bar["trade_date"].strftime(
                                "%Y-%m-%d"
                            )
                            if hasattr(current_bar["trade_date"], "strftime")
                            else str(current_bar["trade_date"]),
                            "entry_price": round(entry_price, 2),
                            "exit_price": round(exit_price, 2),
                            "stop_loss": round(stop_loss, 2),
                            "target": round(target, 2),
                            "shares": shares,
                            "pnl": round(pnl, 2),
                            "pnl_pct": round(pnl_pct, 2),
                            "exit_reason": exit_reason,
                            "hold_days": bars_held,
                        }
                    )
                    running_capital += pnl
                    in_trade = False
                    entry_price = stop_loss = target = 0.0
                    shares = 0

        return trades

    # ------------------------------------------------------------------
    # Strategy signal dispatcher
    # ------------------------------------------------------------------

    def _get_strategy_signal(
        self, df_window: pd.DataFrame, symbol: str, strategy_fn
    ) -> dict | None:
        """
        Generate a signal by calling the pre-resolved strategy function.

        Args:
            df_window: Data available up to current bar (no look-ahead).
            symbol: Symbol name.
            strategy_fn: Callable resolved once in run() — avoids re-instantiation per bar.

        Returns:
            Signal dict (including atr key) or None.
        """
        try:
            return strategy_fn(df_window, symbol)
        except Exception as e:
            logger.debug(f"Signal error for {symbol}: {e}")
            return None

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _calculate_metrics(self, trades: list, initial_capital: float) -> dict:
        """
        Compute aggregate performance metrics from a list of trades.

        Args:
            trades: List of trade dicts from _simulate_symbol.
            initial_capital: Starting capital.

        Returns:
            Metrics dict.
        """
        if not trades:
            return {
                "total_trades": 0,
                "win_count": 0,
                "loss_count": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "total_pnl_pct": 0.0,
                "avg_pnl_per_trade": 0.0,
                "max_drawdown": 0.0,
                "max_drawdown_pct": 0.0,
                "sharpe_ratio": 0.0,
                "profit_factor": 0.0,
                "best_trade": 0.0,
                "worst_trade": 0.0,
                "avg_hold_days": 0.0,
            }

        pnls = [t["pnl"] for t in trades]
        pnl_pcts = [t["pnl_pct"] for t in trades]
        hold_days = [t["hold_days"] for t in trades]

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        total_pnl = sum(pnls)
        total_pnl_pct = (total_pnl / initial_capital) * 100.0

        win_rate = (len(wins) / len(pnls)) * 100.0 if pnls else 0.0
        avg_pnl = total_pnl / len(pnls) if pnls else 0.0

        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        # Sharpe ratio — per-trade basis, annualised by average hold days.
        # Formula: (mean_trade_pct / std_trade_pct) * sqrt(252 / avg_hold_days)
        # This approximates how many independent trade-periods fit in a year.
        if len(pnl_pcts) > 1:
            mean_ret  = np.mean(pnl_pcts)
            std_ret   = np.std(pnl_pcts, ddof=1)
            avg_hold  = (sum(hold_days) / len(hold_days)) if hold_days else 1.0
            periods   = max(252 / max(avg_hold, 1), 1)
            sharpe    = (mean_ret / std_ret) * math.sqrt(periods) if std_ret > 0 else 0.0
        else:
            sharpe = 0.0

        # Max drawdown on equity curve
        equity = initial_capital
        peak = initial_capital
        max_dd = 0.0
        max_dd_pct = 0.0
        for pnl in pnls:
            equity += pnl
            if equity > peak:
                peak = equity
            dd = peak - equity
            dd_pct = (dd / peak) * 100.0 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd_pct

        return {
            "total_trades": len(trades),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "avg_pnl_per_trade": round(avg_pnl, 2),
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd_pct, 2),
            "sharpe_ratio": round(sharpe, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999.0,
            "best_trade": round(max(pnls), 2),
            "worst_trade": round(min(pnls), 2),
            "avg_hold_days": round(sum(hold_days) / len(hold_days), 1) if hold_days else 0.0,
        }

    # ------------------------------------------------------------------
    # Equity curve
    # ------------------------------------------------------------------

    def _build_equity_curve(self, trades: list, initial_capital: float) -> list:
        """
        Build an equity curve list sorted by exit date.

        Returns:
            List of {date: str, equity: float}
        """
        if not trades:
            return [{"date": datetime.today().strftime("%Y-%m-%d"), "equity": initial_capital}]

        # Sort by exit date
        sorted_trades = sorted(trades, key=lambda t: t["exit_date"])

        equity = initial_capital
        curve = [{"date": sorted_trades[0]["entry_date"], "equity": equity}]

        for trade in sorted_trades:
            equity += trade["pnl"]
            curve.append({"date": trade["exit_date"], "equity": round(equity, 2)})

        return curve
