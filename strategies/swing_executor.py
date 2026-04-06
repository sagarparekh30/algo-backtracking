"""
Strategy Manager — scans market data for swing trading signals.

16 strategies:
  1.  golden_rsi        — Bullish pullback: EMA200 > price > pullback, RSI < 40
  2.  sma_cross         — SMA20 freshly crosses above SMA50
  3.  macd_cross        — MACD line crosses above signal line
  4.  bollinger_bounce  — Price at lower Bollinger Band, RSI < 35, above SMA200
  5.  breakout          — Close above 20-day high, volume > 1.5x avg
  6.  volume_surge      — Volume > 2x avg, bullish candle, above SMA50
  7.  golden_cross      — SMA50 freshly crosses above SMA200
  8.  supertrend        — Price crosses above Supertrend (trend flips bullish)
  9.  stochastic        — Stoch %K < 20 crosses above %D, above SMA200
  10. adx_trend         — ADX > 25, +DI > -DI, above SMA50
  11. hammer            — Hammer candlestick pattern in downtrend
  12. bullish_engulfing — Bullish engulfing pattern
  13. squeeze           — TTM Squeeze fires bullish (BB breaks out of KC)
  14. ema_ribbon        — EMA 8 > 21 > 55 alignment, price above all
  15. high_52w          — Close breaks 52-week high with volume confirmation
  16. cci_bounce        — CCI crosses up from below -100
"""

import pandas as pd
import numpy as np
import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import TABLE_NAME, ATR_PERIOD, ATR_MULTIPLIER, REWARD_RISK_RATIO
from db.connection import get_engine
from strategies.indicators import (
    rsi, ema, sma, macd, bollinger_bands, atr, volume_surge,
    stochastic, adx, supertrend, cci, keltner_channels,
)

logger = logging.getLogger(__name__)

STRATEGY_DESCRIPTIONS = {
    "golden_rsi":        "Bullish pullback: price above EMA200 (long-term uptrend) but below EMA20 (short-term pullback) with RSI < 40 (oversold dip)",
    "sma_cross":         "Momentum breakout: SMA20 freshly crosses above SMA50 — short-term momentum overtaking medium-term",
    "macd_cross":        "Trend reversal: MACD line crosses above signal line and histogram turns positive",
    "bollinger_bounce":  "Mean-reversion long: price at/below lower Bollinger Band (2σ), RSI < 35, still above SMA200",
    "breakout":          "Resistance breakout: close above 20-day high with volume > 1.5x average — confirmed by participation",
    "volume_surge":      "Institutional accumulation: volume > 2x 20-day average on a bullish candle above SMA50",
    "golden_cross":      "Major trend signal: SMA50 freshly crosses above SMA200 — 'Golden Cross', strongest long-term buy signal",
    "supertrend":        "ATR-based trend: price just crossed above the Supertrend line (trend flipped from bearish to bullish)",
    "stochastic":        "Oversold reversal: Stochastic %K < 20 (oversold) and %K crosses above %D while above SMA200",
    "adx_trend":         "Strong trend entry: ADX > 25 (trend strength confirmed), +DI > -DI (bulls in control), price above SMA50",
    "hammer":            "Bullish reversal candle: hammer pattern (long lower wick ≥ 2× body, tiny upper wick) in a downtrend",
    "bullish_engulfing": "Two-candle reversal: today's bullish candle completely engulfs yesterday's bearish candle body",
    "squeeze":           "TTM Squeeze: Bollinger Bands were inside Keltner Channels (low volatility) and just broke out bullishly",
    "ema_ribbon":        "EMA alignment: EMA8 > EMA21 > EMA55 (all stacked bullishly) and price above all three — trend alignment",
    "high_52w":          "52-week breakout: close above the highest close in the past 252 trading days with volume > 1.5x average",
    "cci_bounce":        "CCI oversold bounce: CCI crosses up from below -100 with price above SMA50 — momentum recovery",
}


class StrategyManager:
    def __init__(self):
        self.table_name = TABLE_NAME

    # ── Public API ─────────────────────────────────────────────────────

    def get_signals(self, strategy_id: str = "golden_rsi") -> list:
        """Scan all symbols in the database using the specified strategy."""
        signals = []
        strategy_fn = self._get_strategy_fn(strategy_id)
        if strategy_fn is None:
            logger.warning(f"Unknown strategy: {strategy_id}")
            return signals

        try:
            engine  = get_engine()
            symbols = pd.read_sql(
                f"SELECT DISTINCT symbol FROM {self.table_name}", engine
            )["symbol"].tolist()

            for symbol in symbols:
                try:
                    df = pd.read_sql(
                        f"SELECT * FROM {self.table_name} WHERE symbol = %(sym)s ORDER BY trade_date ASC",
                        engine, params={"sym": symbol},
                    )
                    sig = strategy_fn(df, symbol)
                    if sig:
                        signals.append(sig)
                except Exception as e:
                    logger.error(f"Error scanning {symbol} with {strategy_id}: {e}")

        except Exception as e:
            logger.error(f"Strategy scan error ({strategy_id}): {e}")

        return signals

    def get_all_signals(self) -> dict:
        """Run all strategies and return combined results."""
        results = {}
        for strategy_id in STRATEGY_DESCRIPTIONS:
            try:
                results[strategy_id] = self.get_signals(strategy_id)
            except Exception as e:
                logger.error(f"get_all_signals error for {strategy_id}: {e}")
                results[strategy_id] = []
        return results

    # ── Dispatcher ─────────────────────────────────────────────────────

    def _get_strategy_fn(self, strategy_id: str):
        return {
            "golden_rsi":        self._logic_golden_rsi,
            "sma_cross":         self._logic_sma_cross,
            "macd_cross":        self._logic_macd_cross,
            "bollinger_bounce":  self._logic_bollinger_bounce,
            "breakout":          self._logic_breakout,
            "volume_surge":      self._logic_volume_surge,
            "golden_cross":      self._logic_golden_cross,
            "supertrend":        self._logic_supertrend,
            "stochastic":        self._logic_stochastic,
            "adx_trend":         self._logic_adx_trend,
            "hammer":            self._logic_hammer,
            "bullish_engulfing": self._logic_bullish_engulfing,
            "squeeze":           self._logic_squeeze,
            "ema_ribbon":        self._logic_ema_ribbon,
            "high_52w":          self._logic_high_52w,
            "cci_bounce":        self._logic_cci_bounce,
        }.get(strategy_id)

    # ── Shared helpers ─────────────────────────────────────────────────

    def _compute_common(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["atr14"]    = atr(df, period=ATR_PERIOD)
        df["vol_ratio"] = volume_surge(df, window=20)
        df["rsi14"]    = rsi(df["close"], period=14)
        return df

    def _build_signal(self, symbol, df, metric, trend, strategy_name) -> dict:
        latest     = df.iloc[-1]
        price      = float(latest["close"])
        atr_value  = float(latest["atr14"]) if not pd.isna(latest["atr14"]) else 0.0
        stop_loss  = round(price - ATR_MULTIPLIER * atr_value, 2)
        target     = round(price + ATR_MULTIPLIER * atr_value * REWARD_RISK_RATIO, 2)
        rsi_val    = latest.get("rsi14", float("nan"))
        vol_ratio  = latest.get("vol_ratio", float("nan"))
        return {
            "symbol":       symbol,
            "price":        round(price, 2),
            "stop_loss":    stop_loss,
            "target":       target,
            "metric":       metric,
            "trend":        trend,
            "strategy":     strategy_name,
            "rsi":          round(float(rsi_val), 1) if not pd.isna(rsi_val) else None,
            "volume_ratio": round(float(vol_ratio), 2) if not pd.isna(vol_ratio) else None,
            "atr":          round(atr_value, 2),
        }

    # ══════════════════════════════════════════════════════════════════
    # ORIGINAL 6 STRATEGIES
    # ══════════════════════════════════════════════════════════════════

    def _logic_golden_rsi(self, df, symbol):
        if len(df) < 200: return None
        df = self._compute_common(df)
        df["ema200"] = ema(df["close"], 200)
        df["ema20"]  = ema(df["close"], 20)
        lt = df.iloc[-1]
        if lt["close"] > lt["ema200"] and lt["close"] < lt["ema20"] and lt["rsi14"] < 40:
            return self._build_signal(symbol, df, f"RSI: {round(lt['rsi14'],1)}", "Bullish Pullback", "Golden RSI")
        return None

    def _logic_sma_cross(self, df, symbol):
        if len(df) < 50: return None
        df = self._compute_common(df)
        df["sma20"] = sma(df["close"], 20)
        df["sma50"] = sma(df["close"], 50)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if pv["sma20"] <= pv["sma50"] and lt["sma20"] > lt["sma50"]:
            return self._build_signal(symbol, df, "SMA 20/50 Cross", "Momentum Breakout", "SMA Crossover")
        return None

    def _logic_macd_cross(self, df, symbol):
        if len(df) < 50: return None
        df = self._compute_common(df)
        md = macd(df["close"])
        df["macd"], df["macd_sig"], df["macd_hist"] = md["macd"], md["signal"], md["histogram"]
        lt, pv = df.iloc[-1], df.iloc[-2]
        if pv["macd"] <= pv["macd_sig"] and lt["macd"] > lt["macd_sig"] and lt["macd_hist"] > 0:
            return self._build_signal(symbol, df, f"Hist: {round(lt['macd_hist'],3)}", "MACD Bullish Cross", "MACD Crossover")
        return None

    def _logic_bollinger_bounce(self, df, symbol):
        if len(df) < 200: return None
        df = self._compute_common(df)
        bb = bollinger_bands(df["close"], window=20, num_std=2)
        df["bb_lower"] = bb["lower"]
        df["sma200"]   = sma(df["close"], 200)
        lt = df.iloc[-1]
        if lt["close"] <= lt["bb_lower"] and lt["rsi14"] < 35 and lt["close"] > lt["sma200"]:
            return self._build_signal(symbol, df, f"RSI: {round(lt['rsi14'],1)} | BB Touch", "Bollinger Bounce", "Bollinger Bounce")
        return None

    def _logic_breakout(self, df, symbol):
        if len(df) < 22: return None
        df = self._compute_common(df)
        df["high_20"]   = df["high"].shift(1).rolling(20).max()
        df["avg_vol_20"] = df["volume"].shift(1).rolling(20).mean()
        lt = df.iloc[-1]
        if (pd.notna(lt["high_20"]) and lt["close"] > lt["high_20"]
                and pd.notna(lt["avg_vol_20"]) and lt["volume"] > 1.5 * lt["avg_vol_20"]):
            return self._build_signal(symbol, df, f"Vol: {round(lt['vol_ratio'],1)}x | 20D Break", "Resistance Breakout", "Breakout")
        return None

    def _logic_volume_surge(self, df, symbol):
        if len(df) < 50: return None
        df = self._compute_common(df)
        df["sma50"] = sma(df["close"], 50)
        lt = df.iloc[-1]
        if (pd.notna(lt["vol_ratio"]) and lt["vol_ratio"] > 2.0
                and lt["close"] > lt["open"] and lt["close"] > lt["sma50"]):
            return self._build_signal(symbol, df, f"Vol Surge: {round(lt['vol_ratio'],1)}x", "Volume Accumulation", "Volume Surge")
        return None

    # ══════════════════════════════════════════════════════════════════
    # NEW STRATEGIES 7–16
    # ══════════════════════════════════════════════════════════════════

    def _logic_golden_cross(self, df, symbol):
        """SMA50 freshly crosses above SMA200 — major long-term buy signal."""
        if len(df) < 200: return None
        df = self._compute_common(df)
        df["sma50"]  = sma(df["close"], 50)
        df["sma200"] = sma(df["close"], 200)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if pv["sma50"] <= pv["sma200"] and lt["sma50"] > lt["sma200"]:
            return self._build_signal(symbol, df, "SMA 50/200 Golden Cross", "Major Trend Change", "Golden Cross")
        return None

    def _logic_supertrend(self, df, symbol):
        """Price just crossed above the Supertrend line — bullish flip."""
        if len(df) < 30: return None
        df = self._compute_common(df)
        st = supertrend(df, period=10, multiplier=3.0)
        df["st_trend"] = st["trend"]
        df["st_line"]  = st["line"]
        lt, pv = df.iloc[-1], df.iloc[-2]
        # Fresh bullish flip: previous bar downtrend, current bar uptrend
        if pv["st_trend"] == -1 and lt["st_trend"] == 1:
            return self._build_signal(symbol, df,
                f"ST Line: ₹{round(lt['st_line'],1)}", "Supertrend Buy", "Supertrend")
        return None

    def _logic_stochastic(self, df, symbol):
        """Stochastic %K crosses above %D from the oversold zone (<20), above SMA200."""
        if len(df) < 200: return None
        df = self._compute_common(df)
        st = stochastic(df, period=14, smooth=3)
        df["stoch_k"]  = st["k"]
        df["stoch_d"]  = st["d"]
        df["sma200"]   = sma(df["close"], 200)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pv["stoch_k"] < 20 and pv["stoch_k"] <= pv["stoch_d"]
                and lt["stoch_k"] > lt["stoch_d"]
                and lt["close"] > lt["sma200"]):
            return self._build_signal(symbol, df,
                f"Stoch %K: {round(lt['stoch_k'],1)}", "Stochastic Oversold Bounce", "Stochastic")
        return None

    def _logic_adx_trend(self, df, symbol):
        """ADX > 25 with +DI > -DI and price above SMA50 — confirmed strong uptrend."""
        if len(df) < 50: return None
        df = self._compute_common(df)
        adx_data = adx(df, period=14)
        df["adx_val"]  = adx_data["adx"]
        df["plus_di"]  = adx_data["plus_di"]
        df["minus_di"] = adx_data["minus_di"]
        df["sma50"]    = sma(df["close"], 50)
        lt = df.iloc[-1]
        if (pd.notna(lt["adx_val"]) and lt["adx_val"] > 25
                and lt["plus_di"] > lt["minus_di"]
                and lt["close"] > lt["sma50"]):
            return self._build_signal(symbol, df,
                f"ADX: {round(lt['adx_val'],1)} | +DI: {round(lt['plus_di'],1)}",
                "Strong Uptrend", "ADX Trend")
        return None

    def _logic_hammer(self, df, symbol):
        """
        Hammer candle pattern:
          - Lower shadow ≥ 2× real body
          - Upper shadow ≤ 30% of real body
          - Appears after a downtrend (close < SMA20 for past 5 bars)
        """
        if len(df) < 30: return None
        df = self._compute_common(df)
        df["sma20"] = sma(df["close"], 20)
        lt = df.iloc[-1]

        body   = abs(lt["close"] - lt["open"])
        lo_wick = min(lt["open"], lt["close"]) - lt["low"]
        hi_wick = lt["high"] - max(lt["open"], lt["close"])

        if body < 0.001: return None  # doji — skip

        # In downtrend: last 5 closes below SMA20
        recent = df.iloc[-6:-1]
        in_downtrend = (recent["close"] < recent["sma20"]).all()

        if (lo_wick >= 2 * body and hi_wick <= 0.3 * body and in_downtrend):
            return self._build_signal(symbol, df,
                f"Hammer | Lower wick {round(lo_wick/body,1)}× body",
                "Bullish Reversal Candle", "Hammer")
        return None

    def _logic_bullish_engulfing(self, df, symbol):
        """
        Bullish engulfing pattern:
          - Day N-1: bearish candle (close < open)
          - Day N: bullish candle (close > open) that fully engulfs day N-1 body
        """
        if len(df) < 20: return None
        df = self._compute_common(df)
        df["sma50"] = sma(df["close"], 50)
        lt, pv = df.iloc[-1], df.iloc[-2]

        prev_bearish  = pv["close"] < pv["open"]
        curr_bullish  = lt["close"] > lt["open"]
        engulfs       = lt["open"] <= pv["close"] and lt["close"] >= pv["open"]

        # Require meaningful body size (avoid tiny candles)
        prev_body = abs(pv["close"] - pv["open"])
        curr_body = abs(lt["close"] - lt["open"])

        if (prev_bearish and curr_bullish and engulfs
                and curr_body > prev_body * 0.5
                and lt["close"] > lt["sma50"]):
            return self._build_signal(symbol, df,
                f"Engulfs prev body {round(curr_body/prev_body,1)}×",
                "Bullish Engulfing Pattern", "Bullish Engulfing")
        return None

    def _logic_squeeze(self, df, symbol):
        """
        TTM Squeeze — Bollinger Bands break out of Keltner Channels:
          - Squeeze ON: BB upper < KC upper AND BB lower > KC lower
          - Squeeze OFF (signal): previous bar was squeezed, current bar is not
          - Momentum direction: close > midpoint of BB (bullish squeeze release)
        """
        if len(df) < 30: return None
        df = self._compute_common(df)
        bb = bollinger_bands(df["close"], window=20, num_std=2)
        kc = keltner_channels(df, period=20, multiplier=1.5)
        df["bb_upper"] = bb["upper"]
        df["bb_lower"] = bb["lower"]
        df["bb_mid"]   = bb["middle"]
        df["kc_upper"] = kc["upper"]
        df["kc_lower"] = kc["lower"]

        # Squeeze ON when BB is inside KC
        df["squeeze_on"] = (df["bb_upper"] < df["kc_upper"]) & (df["bb_lower"] > df["kc_lower"])

        lt, pv = df.iloc[-1], df.iloc[-2]

        squeeze_fired  = pv["squeeze_on"] and not lt["squeeze_on"]
        bullish_breakout = lt["close"] > lt["bb_mid"]

        if squeeze_fired and bullish_breakout:
            return self._build_signal(symbol, df,
                "TTM Squeeze Released ↑", "Volatility Breakout", "Squeeze")
        return None

    def _logic_ema_ribbon(self, df, symbol):
        """
        EMA ribbon alignment: EMA8 > EMA21 > EMA55 all stacked bullishly
        and close above all three — full alignment.
        """
        if len(df) < 60: return None
        df = self._compute_common(df)
        df["ema8"]  = ema(df["close"], 8)
        df["ema21"] = ema(df["close"], 21)
        df["ema55"] = ema(df["close"], 55)
        lt, pv = df.iloc[-1], df.iloc[-2]

        # Freshly aligned: previous bar not fully aligned, current is
        prev_aligned = pv["ema8"] > pv["ema21"] > pv["ema55"]
        curr_aligned = lt["ema8"] > lt["ema21"] > lt["ema55"] and lt["close"] > lt["ema8"]

        if curr_aligned and not prev_aligned:
            return self._build_signal(symbol, df,
                f"EMA 8/21/55 Aligned | {round(lt['ema8'],0)}/{round(lt['ema21'],0)}/{round(lt['ema55'],0)}",
                "EMA Ribbon Alignment", "EMA Ribbon")
        return None

    def _logic_high_52w(self, df, symbol):
        """
        52-week high breakout: close > highest close in past 252 bars,
        with volume > 1.5x 20-day average.
        """
        if len(df) < 255: return None
        df = self._compute_common(df)
        # 52-week high = max close of bars before today
        df["high_52w"]  = df["close"].shift(1).rolling(252).max()
        df["avg_vol_20"] = df["volume"].shift(1).rolling(20).mean()
        lt = df.iloc[-1]
        if (pd.notna(lt["high_52w"])
                and lt["close"] > lt["high_52w"]
                and lt["volume"] > 1.5 * lt["avg_vol_20"]):
            return self._build_signal(symbol, df,
                f"52W High: ₹{round(lt['high_52w'],0)} | Vol {round(lt['vol_ratio'],1)}x",
                "52-Week Breakout", "52W High Breakout")
        return None

    def _logic_cci_bounce(self, df, symbol):
        """
        CCI oversold bounce: CCI crosses up from below -100 while price > SMA50.
        """
        if len(df) < 50: return None
        df = self._compute_common(df)
        df["cci20"] = cci(df, period=20)
        df["sma50"] = sma(df["close"], 50)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(pv["cci20"]) and pd.notna(lt["cci20"])
                and pv["cci20"] <= -100 and lt["cci20"] > -100
                and lt["close"] > lt["sma50"]):
            return self._build_signal(symbol, df,
                f"CCI: {round(lt['cci20'],1)} ↑ from oversold",
                "CCI Oversold Bounce", "CCI Bounce")
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    manager = StrategyManager()
    for strat_id in STRATEGY_DESCRIPTIONS:
        sigs = manager.get_signals(strat_id)
        print(f"{strat_id:25s} → {len(sigs)} signal(s)")
