"""
Strategy Manager — scans market data for swing trading signals.

48 strategies across MA, RSI, MACD, Bollinger, Volume, Stochastic,
Fibonacci, ADX, Ichimoku, Supertrend, Advanced, and Multi-Indicator combos.
"""

import pandas as pd
import numpy as np
import os
import sys
import logging
import time as _time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import TABLE_NAME, ATR_PERIOD, ATR_MULTIPLIER, REWARD_RISK_RATIO
from db.connection import get_engine
from strategies.indicators import (
    rsi, ema, sma, macd, bollinger_bands, atr, volume_surge,
    stochastic, adx, supertrend, cci, keltner_channels,
    obv, accumulation_distribution, vwap, parabolic_sar,
    williams_r, ichimoku, donchian_channel, pivot_points,
)

logger = logging.getLogger(__name__)

# Module-level cache for get_all_signals — 15-minute TTL
_all_signals_cache: dict | None = None
_all_signals_cache_ts: float = 0.0
_ALL_SIGNALS_TTL = 900.0   # 15 minutes

STRATEGY_DESCRIPTIONS = {
    # ── Moving Average ──────────────────────────────────────────────────
    "golden_rsi":        "EMA200 uptrend + EMA20 pullback + RSI < 40 — buy-the-dip in uptrend",
    "sma_cross":         "SMA20 freshly crosses above SMA50 — short-term momentum breakout",
    "golden_cross":      "SMA50 freshly crosses above SMA200 — major long-term buy signal",
    "ema_ribbon":        "EMA8 > EMA21 > EMA55 fully stacked + price above all three",
    "triple_ma":         "EMA9 crosses EMA21, both above EMA55 — triple confirmation entry",
    "ma_price_action":   "Price reclaims EMA20 from below with a bullish candle + volume",

    # ── RSI ────────────────────────────────────────────────────────────
    "rsi_50_trend":      "RSI crosses above 50 midline confirming trend shift to bullish",
    "rsi_divergence":    "Bullish RSI divergence: price lower low, RSI higher low — hidden strength",
    "rsi_swing_reject":  "RSI dips below 30 then quickly rejects back above — failed bearish move",
    "rsi_support":       "RSI bounce (35-45) while price sits on key support (SMA50 or SMA200)",

    # ── MACD ───────────────────────────────────────────────────────────
    "macd_cross":        "MACD line crosses above signal line with histogram turning positive",
    "macd_zero_line":    "MACD line crosses above zero — trend officially turns bullish",
    "macd_divergence":   "Bullish MACD divergence: price lower low but MACD higher low",
    "macd_histogram":    "MACD histogram flips from negative to positive (momentum shift)",
    "macd_trend":        "MACD crossover occurring above zero line — strong trend confirmation",

    # ── Bollinger Bands ─────────────────────────────────────────────────
    "bollinger_bounce":  "Price at/below lower BB (2σ), RSI < 35, above SMA200 — mean reversion",
    "squeeze":           "TTM Squeeze: BB breaks out of Keltner Channel — low-volatility explosion",
    "double_bb":         "Price between BB 1σ and 2σ lower bands — high-probability entry zone",

    # ── Volume ──────────────────────────────────────────────────────────
    "breakout":          "Close above 20-day high with volume > 1.5x average — confirmed breakout",
    "volume_surge":      "Volume > 2x average on bullish candle above SMA50 — institutional buy",
    "obv_trend":         "OBV making new 20-day high while price is in uptrend — volume confirms",
    "vwap_bounce":       "Price pulls back to rolling VWAP and bounces with volume — institutional level",
    "acc_dist":          "A/D Line rising with price above SMA50 — accumulation in progress",

    # ── Stochastic ──────────────────────────────────────────────────────
    "stochastic":        "Stoch %K < 20 crosses above %D while price above SMA200 — oversold reversal",
    "stoch_divergence":  "Bullish Stochastic divergence: price lower low, Stoch %K higher low",
    "stoch_trend":       "Stochastic %K crosses %D in 20-50 range (not overbought) above EMA200",

    # ── Fibonacci ───────────────────────────────────────────────────────
    "fib_retracement":   "Price retraces to 38.2%–61.8% Fibonacci level of recent swing — confluence entry",
    "fib_trend":         "Fibonacci 61.8% retracement + RSI 40-50 + above EMA200 — three-way confluence",

    # ── ADX ─────────────────────────────────────────────────────────────
    "adx_trend":         "ADX > 25 (strong trend), +DI > -DI (bulls leading), price above SMA50",
    "adx_breakout":      "ADX rising above 20 + price breaks 20-day high — trend strengthening on breakout",

    # ── Ichimoku ────────────────────────────────────────────────────────
    "ichimoku_cloud":    "Price breaks above Ichimoku Cloud (both Senkou A & B) — full bullish signal",
    "tenkan_kijun_cross":"Tenkan-sen (9) crosses above Kijun-sen (26) above the cloud — TK cross",
    "kumo_twist":        "Senkou A crosses above Senkou B (Kumo twist) — cloud turning bullish",

    # ── Supertrend ──────────────────────────────────────────────────────
    "supertrend":        "Price crosses above Supertrend line — ATR-based trend flip to bullish",
    "supertrend_ema":    "Supertrend bullish + EMA50 rising + price above EMA200 — triple alignment",
    "supertrend_volume": "Supertrend bullish flip with volume > 1.5x average — confirmed reversal",
    "supertrend_adx":    "Supertrend bullish + ADX > 25 — strong trending move confirmed",

    # ── Advanced / Single Indicator ─────────────────────────────────────
    "cci_bounce":        "CCI crosses up from below -100 with price above SMA50 — momentum recovery",
    "parabolic_sar":     "Parabolic SAR flips from above to below price — trend reversal signal",
    "donchian_breakout": "Price breaks above Donchian upper channel (20-day) with volume surge",
    "williams_r_bounce": "Williams %R crosses up from below -80 while above SMA50 — oversold bounce",
    "pivot_bounce":      "Price bouncing from S1/S2 pivot support with RSI < 45 — support hold",

    # ── Multi-Indicator Combos ───────────────────────────────────────────
    "rsi_macd":          "RSI > 50 AND MACD crossover both aligned bullish — dual momentum signal",
    "ema_rsi_volume":    "EMA200 uptrend + RSI 40-60 reset + volume spike — institutional accumulation",
    "bb_rsi":            "Lower Bollinger Band touch + RSI < 40 + above SMA200 — mean reversion combo",
    "vwap_price_action": "Price above VWAP + bullish engulfing/hammer candle — institutional + PA",
    "supertrend_adx_vol":"Supertrend bullish + ADX > 25 + volume > 1.5x — full-strength trend signal",
    "rsi_fibonacci":     "Price at Fibonacci 61.8% + RSI 35-50 bounce — precision entry confluence",
    "ichimoku_volume":   "Price above Ichimoku Cloud + volume surge > 1.5x — cloud breakout confirmed",

    # ── 52W High ────────────────────────────────────────────────────────
    "high_52w":          "Close breaks 52-week high with volume > 1.5x — momentum continuation",
}


class StrategyManager:
    def __init__(self):
        self.table_name = TABLE_NAME

    # ── Public API ─────────────────────────────────────────────────────

    # ── how many calendar days of history each scan needs ────────────────
    _LOOKBACK_DAYS = 730   # ~505 trading days — enough for EMA200 + any indicator

    def get_signals(self, strategy_id: str = "golden_rsi") -> list:
        """Scan all symbols using the specified strategy.

        Uses a single DB query (all symbols, last 2 years) then groups
        in memory — avoids 100+ round-trips that caused the slowness.
        """
        signals = []
        strategy_fn = self._get_strategy_fn(strategy_id)
        if strategy_fn is None:
            logger.warning(f"Unknown strategy: {strategy_id}")
            return signals
        try:
            engine = get_engine()
            df_all = pd.read_sql(
                f"""
                SELECT symbol, trade_date, open, high, low, close, volume
                FROM {self.table_name}
                WHERE trade_date >= CURRENT_DATE - INTERVAL '{self._LOOKBACK_DAYS} days'
                ORDER BY symbol, trade_date ASC
                """,
                engine,
            )
            if df_all.empty:
                return signals
            for symbol, df in df_all.groupby("symbol", sort=False):
                try:
                    df = df.reset_index(drop=True)
                    sig = strategy_fn(df, symbol)
                    if sig:
                        signals.append(sig)
                except Exception as e:
                    logger.error(f"Error scanning {symbol} with {strategy_id}: {e}")
        except Exception as e:
            logger.error(f"Strategy scan error ({strategy_id}): {e}")
        return signals

    def get_all_signals(self, force: bool = False) -> dict:
        """Run all strategies and return combined results.

        Fetches candle data once, then runs every strategy in-memory —
        avoids N separate DB round-trips (one per strategy).
        Results are cached for 15 minutes (configurable via _ALL_SIGNALS_TTL).
        """
        global _all_signals_cache, _all_signals_cache_ts
        now = _time.monotonic()
        if not force and _all_signals_cache and (now - _all_signals_cache_ts) < _ALL_SIGNALS_TTL:
            return _all_signals_cache

        # ── Fetch once ────────────────────────────────────────────────────
        try:
            engine = get_engine()
            df_all = pd.read_sql(
                f"""
                SELECT symbol, trade_date, open, high, low, close, volume
                FROM {self.table_name}
                WHERE trade_date >= CURRENT_DATE - INTERVAL '{self._LOOKBACK_DAYS} days'
                ORDER BY symbol, trade_date ASC
                """,
                engine,
            )
        except Exception as e:
            logger.error(f"get_all_signals: DB fetch failed: {e}")
            return {sid: [] for sid in STRATEGY_DESCRIPTIONS}

        if df_all.empty:
            return {sid: [] for sid in STRATEGY_DESCRIPTIONS}

        # Pre-group by symbol so each strategy just iterates the map
        symbol_groups = {
            sym: grp.reset_index(drop=True)
            for sym, grp in df_all.groupby("symbol", sort=False)
        }

        # ── Run each strategy over the cached data ────────────────────────
        results = {}
        for strategy_id in STRATEGY_DESCRIPTIONS:
            strategy_fn = self._get_strategy_fn(strategy_id)
            if strategy_fn is None:
                results[strategy_id] = []
                continue
            signals = []
            for symbol, df in symbol_groups.items():
                try:
                    sig = strategy_fn(df, symbol)
                    if sig:
                        signals.append(sig)
                except Exception as e:
                    logger.error(f"get_all_signals error for {symbol}/{strategy_id}: {e}")
            results[strategy_id] = signals

        _all_signals_cache = results
        _all_signals_cache_ts = _time.monotonic()
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
            # New strategies
            "triple_ma":         self._logic_triple_ma,
            "ma_price_action":   self._logic_ma_price_action,
            "rsi_50_trend":      self._logic_rsi_50_trend,
            "rsi_divergence":    self._logic_rsi_divergence,
            "rsi_swing_reject":  self._logic_rsi_swing_reject,
            "rsi_support":       self._logic_rsi_support,
            "macd_zero_line":    self._logic_macd_zero_line,
            "macd_divergence":   self._logic_macd_divergence,
            "macd_histogram":    self._logic_macd_histogram,
            "macd_trend":        self._logic_macd_trend,
            "double_bb":         self._logic_double_bb,
            "obv_trend":         self._logic_obv_trend,
            "vwap_bounce":       self._logic_vwap_bounce,
            "acc_dist":          self._logic_acc_dist,
            "stoch_divergence":  self._logic_stoch_divergence,
            "stoch_trend":       self._logic_stoch_trend,
            "fib_retracement":   self._logic_fib_retracement,
            "fib_trend":         self._logic_fib_trend,
            "adx_breakout":      self._logic_adx_breakout,
            "ichimoku_cloud":    self._logic_ichimoku_cloud,
            "tenkan_kijun_cross":self._logic_tenkan_kijun_cross,
            "kumo_twist":        self._logic_kumo_twist,
            "supertrend_ema":    self._logic_supertrend_ema,
            "supertrend_volume": self._logic_supertrend_volume,
            "supertrend_adx":    self._logic_supertrend_adx,
            "parabolic_sar":     self._logic_parabolic_sar,
            "donchian_breakout": self._logic_donchian_breakout,
            "williams_r_bounce": self._logic_williams_r_bounce,
            "pivot_bounce":      self._logic_pivot_bounce,
            "rsi_macd":          self._logic_rsi_macd,
            "ema_rsi_volume":    self._logic_ema_rsi_volume,
            "bb_rsi":            self._logic_bb_rsi,
            "vwap_price_action": self._logic_vwap_price_action,
            "supertrend_adx_vol":self._logic_supertrend_adx_vol,
            "rsi_fibonacci":     self._logic_rsi_fibonacci,
            "ichimoku_volume":   self._logic_ichimoku_volume,
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

    def _bullish_divergence(self, price_series, indicator_series, lookback=20) -> bool:
        """Detect bullish divergence: price makes lower low, indicator makes higher low."""
        if len(price_series) < lookback:
            return False
        p = price_series.iloc[-lookback:]
        i = indicator_series.iloc[-lookback:]
        if p.isna().any() or i.isna().any():
            return False
        p_min_idx   = p.iloc[:-3].idxmin()
        i_at_p_min  = i.loc[p_min_idx]
        p_recent_min = p.iloc[-3:].min()
        i_recent_min = i.iloc[-3:].min()
        # Price made a lower low, indicator made a higher low
        return (p_recent_min < p.loc[p_min_idx]) and (i_recent_min > i_at_p_min)

    # ══════════════════════════════════════════════════════════════════
    # ORIGINAL 16 STRATEGIES
    # ══════════════════════════════════════════════════════════════════

    def _logic_golden_rsi(self, df, symbol):
        """EMA200 uptrend + EMA20 pullback + RSI < 40."""
        if len(df) < 200: return None
        df = self._compute_common(df)
        df["ema20"]  = ema(df["close"], 20)
        df["ema200"] = ema(df["close"], 200)
        lt = df.iloc[-1]
        if (lt["close"] > lt["ema200"] and lt["close"] < lt["ema20"]
                and pd.notna(lt["rsi14"]) and lt["rsi14"] < 40):
            return self._build_signal(symbol, df,
                f"RSI: {round(lt['rsi14'],1)}, above EMA200",
                "Bullish Pullback", "Golden RSI")
        return None

    def _logic_sma_cross(self, df, symbol):
        """SMA20 freshly crosses above SMA50."""
        if len(df) < 50: return None
        df = self._compute_common(df)
        df["sma20"] = sma(df["close"], 20)
        df["sma50"] = sma(df["close"], 50)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["sma20"]) and pd.notna(lt["sma50"])
                and pv["sma20"] <= pv["sma50"] and lt["sma20"] > lt["sma50"]):
            return self._build_signal(symbol, df,
                f"SMA20={round(lt['sma20'],1)} > SMA50={round(lt['sma50'],1)}",
                "Momentum Breakout", "SMA Cross")
        return None

    def _logic_macd_cross(self, df, symbol):
        """MACD line crosses above signal line."""
        if len(df) < 35: return None
        df = self._compute_common(df)
        m = macd(df["close"])
        df["macd_l"], df["macd_s"], df["macd_h"] = m["macd"], m["signal"], m["histogram"]
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["macd_l"]) and pd.notna(lt["macd_s"])
                and pv["macd_l"] <= pv["macd_s"] and lt["macd_l"] > lt["macd_s"]
                and lt["macd_h"] > 0):
            return self._build_signal(symbol, df,
                f"MACD: {round(lt['macd_l'],3)} > Signal: {round(lt['macd_s'],3)}",
                "Trend Reversal", "MACD Cross")
        return None

    def _logic_bollinger_bounce(self, df, symbol):
        """Price at lower Bollinger Band + RSI < 35 + above SMA200."""
        if len(df) < 200: return None
        df = self._compute_common(df)
        bb = bollinger_bands(df["close"])
        df["bb_lower"]  = bb["lower"]
        df["sma200"]    = sma(df["close"], 200)
        lt = df.iloc[-1]
        if (pd.notna(lt["bb_lower"]) and lt["close"] <= lt["bb_lower"] * 1.005
                and lt["rsi14"] < 35 and lt["close"] > lt["sma200"]):
            return self._build_signal(symbol, df,
                f"At BB lower, RSI={round(lt['rsi14'],1)}",
                "Mean Reversion", "Bollinger Bounce")
        return None

    def _logic_breakout(self, df, symbol):
        """Close above 20-day high with volume > 1.5x avg."""
        if len(df) < 25: return None
        df = self._compute_common(df)
        df["high20"] = df["close"].shift(1).rolling(20).max()
        lt = df.iloc[-1]
        if (pd.notna(lt["high20"]) and lt["close"] > lt["high20"]
                and lt["vol_ratio"] > 1.5):
            return self._build_signal(symbol, df,
                f"Break {round(lt['high20'],1)}, Vol={round(lt['vol_ratio'],1)}x",
                "Resistance Breakout", "Breakout")
        return None

    def _logic_volume_surge(self, df, symbol):
        """Volume > 2x avg on bullish candle above SMA50."""
        if len(df) < 25: return None
        df = self._compute_common(df)
        df["sma50"] = sma(df["close"], 50)
        lt = df.iloc[-1]
        if (lt["vol_ratio"] > 2.0 and lt["close"] > lt["open"]
                and lt["close"] > lt["sma50"]):
            return self._build_signal(symbol, df,
                f"Vol={round(lt['vol_ratio'],1)}x surge, bullish candle",
                "Institutional Buy", "Volume Surge")
        return None

    def _logic_golden_cross(self, df, symbol):
        """SMA50 freshly crosses above SMA200."""
        if len(df) < 200: return None
        df = self._compute_common(df)
        df["sma50"]  = sma(df["close"], 50)
        df["sma200"] = sma(df["close"], 200)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["sma50"]) and pd.notna(lt["sma200"])
                and pv["sma50"] <= pv["sma200"] and lt["sma50"] > lt["sma200"]):
            return self._build_signal(symbol, df,
                f"SMA50={round(lt['sma50'],1)} crossed SMA200={round(lt['sma200'],1)}",
                "Golden Cross", "Golden Cross")
        return None

    def _logic_supertrend(self, df, symbol):
        """Price crosses above Supertrend (trend flips bullish)."""
        if len(df) < 15: return None
        df = self._compute_common(df)
        st = supertrend(df)
        df["st_trend"] = st["trend"]
        lt, pv = df.iloc[-1], df.iloc[-2]
        if pv["st_trend"] == -1 and lt["st_trend"] == 1:
            return self._build_signal(symbol, df,
                f"Supertrend flipped bullish at {round(lt['close'],1)}",
                "Trend Flip", "Supertrend")
        return None

    def _logic_stochastic(self, df, symbol):
        """Stoch %K < 20 crosses above %D while above SMA200."""
        if len(df) < 200: return None
        df = self._compute_common(df)
        st = stochastic(df)
        df["stoch_k"], df["stoch_d"] = st["k"], st["d"]
        df["sma200"] = sma(df["close"], 200)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["stoch_k"]) and pd.notna(lt["stoch_d"])
                and pv["stoch_k"] < 20 and pv["stoch_k"] <= pv["stoch_d"]
                and lt["stoch_k"] > lt["stoch_d"]
                and lt["close"] > lt["sma200"]):
            return self._build_signal(symbol, df,
                f"Stoch %K={round(lt['stoch_k'],1)} crossed %D from oversold",
                "Oversold Reversal", "Stochastic")
        return None

    def _logic_adx_trend(self, df, symbol):
        """ADX > 25, +DI > -DI, price above SMA50."""
        if len(df) < 50: return None
        df = self._compute_common(df)
        a = adx(df)
        df["adx14"]    = a["adx"]
        df["plus_di"]  = a["plus_di"]
        df["minus_di"] = a["minus_di"]
        df["sma50"]    = sma(df["close"], 50)
        lt = df.iloc[-1]
        if (pd.notna(lt["adx14"]) and lt["adx14"] > 25
                and lt["plus_di"] > lt["minus_di"]
                and lt["close"] > lt["sma50"]):
            return self._build_signal(symbol, df,
                f"ADX={round(lt['adx14'],1)}, +DI={round(lt['plus_di'],1)}",
                "Strong Trend", "ADX Trend")
        return None

    def _logic_hammer(self, df, symbol):
        """Hammer candlestick in downtrend (EMA20 < EMA50)."""
        if len(df) < 50: return None
        df = self._compute_common(df)
        df["ema20"] = ema(df["close"], 20)
        df["ema50"] = ema(df["close"], 50)
        lt = df.iloc[-1]
        body   = abs(lt["close"] - lt["open"])
        rng    = lt["high"] - lt["low"]
        if rng == 0: return None
        lower_wick = min(lt["close"], lt["open"]) - lt["low"]
        upper_wick = lt["high"] - max(lt["close"], lt["open"])
        is_hammer  = (lower_wick >= 2 * body and upper_wick <= 0.3 * rng
                      and rng > 0.001 * lt["close"])
        in_downtrend = lt["ema20"] < lt["ema50"]
        if is_hammer and in_downtrend:
            return self._build_signal(symbol, df,
                f"Hammer: lower wick={round(lower_wick/rng*100)}% of range",
                "Bullish Reversal Candle", "Hammer")
        return None

    def _logic_bullish_engulfing(self, df, symbol):
        """Bullish engulfing: today's bull candle engulfs yesterday's bear candle."""
        if len(df) < 3: return None
        df = self._compute_common(df)
        lt, pv = df.iloc[-1], df.iloc[-2]
        is_engulfing = (pv["close"] < pv["open"] and lt["close"] > lt["open"]
                        and lt["close"] > pv["open"] and lt["open"] < pv["close"])
        if is_engulfing:
            return self._build_signal(symbol, df,
                f"Engulfed {round(pv['open'],1)}–{round(pv['close'],1)} bear candle",
                "Two-Candle Reversal", "Bullish Engulfing")
        return None

    def _logic_squeeze(self, df, symbol):
        """TTM Squeeze: BB inside Keltner then BB breaks out bullishly."""
        if len(df) < 25: return None
        df = self._compute_common(df)
        bb = bollinger_bands(df["close"])
        kc = keltner_channels(df)
        df["bb_upper"], df["bb_lower"] = bb["upper"], bb["lower"]
        df["kc_upper"], df["kc_lower"] = kc["upper"], kc["lower"]
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["bb_upper"]) and pd.notna(lt["kc_upper"])):
            was_squeezed = (pv["bb_upper"] < pv["kc_upper"] and pv["bb_lower"] > pv["kc_lower"])
            broke_out    = lt["bb_upper"] >= lt["kc_upper"]
            bullish      = lt["close"] > lt["open"]
            if was_squeezed and broke_out and bullish:
                return self._build_signal(symbol, df,
                    f"BB squeezed KC, now breaking out bullish",
                    "Volatility Breakout", "TTM Squeeze")
        return None

    def _logic_ema_ribbon(self, df, symbol):
        """EMA8 > EMA21 > EMA55 and price above all three."""
        if len(df) < 55: return None
        df = self._compute_common(df)
        df["ema8"]  = ema(df["close"], 8)
        df["ema21"] = ema(df["close"], 21)
        df["ema55"] = ema(df["close"], 55)
        lt = df.iloc[-1]
        if (pd.notna(lt["ema55"])
                and lt["ema8"] > lt["ema21"] > lt["ema55"]
                and lt["close"] > lt["ema8"]):
            return self._build_signal(symbol, df,
                f"EMA8={round(lt['ema8'],1)} > EMA21={round(lt['ema21'],1)} > EMA55={round(lt['ema55'],1)}",
                "EMA Alignment", "EMA Ribbon")
        return None

    def _logic_high_52w(self, df, symbol):
        """Close breaks 52-week high with volume > 1.5x."""
        if len(df) < 252: return None
        df = self._compute_common(df)
        df["high52"] = df["close"].shift(1).rolling(252).max()
        lt = df.iloc[-1]
        if (pd.notna(lt["high52"]) and lt["close"] > lt["high52"]
                and lt["vol_ratio"] > 1.5):
            return self._build_signal(symbol, df,
                f"New 52W high: {round(lt['close'],1)} > {round(lt['high52'],1)}",
                "52-Week Breakout", "52W High")
        return None

    def _logic_cci_bounce(self, df, symbol):
        """CCI crosses up from below -100 while price > SMA50."""
        if len(df) < 50: return None
        df = self._compute_common(df)
        df["cci20"] = cci(df, period=20)
        df["sma50"] = sma(df["close"], 50)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(pv["cci20"]) and pd.notna(lt["cci20"])
                and pv["cci20"] <= -100 and lt["cci20"] > -100
                and lt["close"] > lt["sma50"]):
            return self._build_signal(symbol, df,
                f"CCI={round(lt['cci20'],1)} ↑ from oversold",
                "CCI Oversold Bounce", "CCI Bounce")
        return None

    # ══════════════════════════════════════════════════════════════════
    # NEW STRATEGIES — MA GROUP
    # ══════════════════════════════════════════════════════════════════

    def _logic_triple_ma(self, df, symbol):
        """EMA9 crosses EMA21, both above EMA55 — triple MA confirmation."""
        if len(df) < 60: return None
        df = self._compute_common(df)
        df["ema9"]  = ema(df["close"], 9)
        df["ema21"] = ema(df["close"], 21)
        df["ema55"] = ema(df["close"], 55)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["ema55"])
                and pv["ema9"] <= pv["ema21"]
                and lt["ema9"] > lt["ema21"]
                and lt["ema21"] > lt["ema55"]
                and lt["close"] > lt["ema9"]):
            return self._build_signal(symbol, df,
                f"EMA9 crossed EMA21, both > EMA55",
                "Triple MA Bullish", "Triple MA")
        return None

    def _logic_ma_price_action(self, df, symbol):
        """Price reclaims EMA20 from below with a bullish candle + above-avg volume."""
        if len(df) < 25: return None
        df = self._compute_common(df)
        df["ema20"] = ema(df["close"], 20)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["ema20"])
                and pv["close"] < pv["ema20"]
                and lt["close"] > lt["ema20"]
                and lt["close"] > lt["open"]
                and lt["vol_ratio"] > 1.2):
            return self._build_signal(symbol, df,
                f"Reclaimed EMA20={round(lt['ema20'],1)} with bullish candle",
                "MA Reclaim", "MA + Price Action")
        return None

    # ══════════════════════════════════════════════════════════════════
    # NEW STRATEGIES — RSI GROUP
    # ══════════════════════════════════════════════════════════════════

    def _logic_rsi_50_trend(self, df, symbol):
        """RSI crosses above 50 — trend shifts bullish."""
        if len(df) < 20: return None
        df = self._compute_common(df)
        df["ema50"] = ema(df["close"], 50)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["rsi14"])
                and pv["rsi14"] <= 50 and lt["rsi14"] > 50
                and lt["close"] > lt["ema50"]):
            return self._build_signal(symbol, df,
                f"RSI={round(lt['rsi14'],1)} crossed above 50 midline",
                "Trend Shift Bullish", "RSI 50 Level")
        return None

    def _logic_rsi_divergence(self, df, symbol):
        """Bullish RSI divergence: price lower low, RSI higher low."""
        if len(df) < 30: return None
        df = self._compute_common(df)
        df["sma50"] = sma(df["close"], 50)
        lt = df.iloc[-1]
        if pd.isna(lt["rsi14"]) or lt["close"] < lt["sma50"]:
            return None
        if self._bullish_divergence(df["close"].iloc[-25:], df["rsi14"].iloc[-25:], lookback=20):
            return self._build_signal(symbol, df,
                f"RSI divergence: price ↓↓, RSI ↑ (hidden strength)",
                "Bullish Divergence", "RSI Divergence")
        return None

    def _logic_rsi_swing_reject(self, df, symbol):
        """RSI dips below 30, quickly rejects back above — failed bear move."""
        if len(df) < 10: return None
        df = self._compute_common(df)
        window = df.iloc[-6:]
        rsi_vals = window["rsi14"]
        if rsi_vals.isna().any():
            return None
        lt = df.iloc[-1]
        dipped_below_30 = (rsi_vals.iloc[:-1] < 30).any()
        now_above_35    = lt["rsi14"] > 35
        close_above_open = lt["close"] > lt["open"]
        if dipped_below_30 and now_above_35 and close_above_open:
            return self._build_signal(symbol, df,
                f"RSI={round(lt['rsi14'],1)}: rejected below-30 dip",
                "RSI Swing Rejection", "RSI Swing Reject")
        return None

    def _logic_rsi_support(self, df, symbol):
        """RSI 35-45 bounce while price sits on SMA50 or SMA200 support."""
        if len(df) < 200: return None
        df = self._compute_common(df)
        df["sma50"]  = sma(df["close"], 50)
        df["sma200"] = sma(df["close"], 200)
        lt = df.iloc[-1]
        rsi_in_range = 35 <= lt["rsi14"] <= 50
        on_sma50     = abs(lt["close"] - lt["sma50"])  / lt["sma50"]  < 0.015
        on_sma200    = abs(lt["close"] - lt["sma200"]) / lt["sma200"] < 0.015
        if rsi_in_range and (on_sma50 or on_sma200):
            support = "SMA50" if on_sma50 else "SMA200"
            return self._build_signal(symbol, df,
                f"RSI={round(lt['rsi14'],1)} bounce at {support} support",
                "RSI + Support", "RSI Support")
        return None

    # ══════════════════════════════════════════════════════════════════
    # NEW STRATEGIES — MACD GROUP
    # ══════════════════════════════════════════════════════════════════

    def _logic_macd_zero_line(self, df, symbol):
        """MACD line crosses above zero — trend officially turns bullish."""
        if len(df) < 35: return None
        df = self._compute_common(df)
        m = macd(df["close"])
        df["macd_l"] = m["macd"]
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["macd_l"])
                and pv["macd_l"] <= 0 and lt["macd_l"] > 0):
            return self._build_signal(symbol, df,
                f"MACD={round(lt['macd_l'],3)} crossed zero line",
                "Trend Turns Bullish", "MACD Zero Line")
        return None

    def _logic_macd_divergence(self, df, symbol):
        """Bullish MACD divergence: price lower low, MACD higher low."""
        if len(df) < 40: return None
        df = self._compute_common(df)
        m = macd(df["close"])
        df["macd_l"] = m["macd"]
        lt = df.iloc[-1]
        if pd.isna(lt["macd_l"]):
            return None
        if self._bullish_divergence(df["close"].iloc[-25:], df["macd_l"].iloc[-25:], lookback=20):
            return self._build_signal(symbol, df,
                f"MACD divergence: price ↓↓, MACD ↑",
                "MACD Bullish Divergence", "MACD Divergence")
        return None

    def _logic_macd_histogram(self, df, symbol):
        """MACD histogram flips from negative to positive."""
        if len(df) < 35: return None
        df = self._compute_common(df)
        m = macd(df["close"])
        df["macd_h"] = m["histogram"]
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["macd_h"])
                and pv["macd_h"] < 0 and lt["macd_h"] > 0):
            return self._build_signal(symbol, df,
                f"MACD histogram flipped +{round(lt['macd_h'],4)}",
                "Momentum Shift", "MACD Histogram")
        return None

    def _logic_macd_trend(self, df, symbol):
        """MACD crossover above zero line — strong trend confirmation."""
        if len(df) < 35: return None
        df = self._compute_common(df)
        m = macd(df["close"])
        df["macd_l"], df["macd_s"] = m["macd"], m["signal"]
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["macd_l"]) and lt["macd_l"] > 0
                and pv["macd_l"] <= pv["macd_s"]
                and lt["macd_l"] > lt["macd_s"]):
            return self._build_signal(symbol, df,
                f"MACD crossover above zero: {round(lt['macd_l'],3)}",
                "Strong Trend Confirmed", "MACD + Trend")
        return None

    # ══════════════════════════════════════════════════════════════════
    # NEW STRATEGIES — BOLLINGER BANDS
    # ══════════════════════════════════════════════════════════════════

    def _logic_double_bb(self, df, symbol):
        """Price in high-probability zone: between 1σ and 2σ lower bands."""
        if len(df) < 25: return None
        df = self._compute_common(df)
        bb2 = bollinger_bands(df["close"], num_std=2.0)
        bb1 = bollinger_bands(df["close"], num_std=1.0)
        df["bb2_lower"] = bb2["lower"]
        df["bb1_lower"] = bb1["lower"]
        df["sma50"]     = sma(df["close"], 50)
        lt = df.iloc[-1]
        in_zone = lt["bb2_lower"] <= lt["close"] <= lt["bb1_lower"]
        if (pd.notna(lt["bb2_lower"]) and in_zone
                and lt["close"] > lt["sma50"]
                and lt["rsi14"] < 45):
            return self._build_signal(symbol, df,
                f"Between 1σ ({round(lt['bb1_lower'],1)}) and 2σ ({round(lt['bb2_lower'],1)}) lower BB",
                "Double BB Entry Zone", "Double Bollinger Band")
        return None

    # ══════════════════════════════════════════════════════════════════
    # NEW STRATEGIES — VOLUME GROUP
    # ══════════════════════════════════════════════════════════════════

    def _logic_obv_trend(self, df, symbol):
        """OBV making new 20-day high while price is above EMA50."""
        if len(df) < 25: return None
        df = self._compute_common(df)
        df["obv"]   = obv(df)
        df["ema50"] = ema(df["close"], 50)
        df["obv_high20"] = df["obv"].shift(1).rolling(20).max()
        lt = df.iloc[-1]
        if (pd.notna(lt["obv_high20"])
                and lt["obv"] > lt["obv_high20"]
                and lt["close"] > lt["ema50"]
                and lt["close"] > lt["open"]):
            return self._build_signal(symbol, df,
                f"OBV new 20D high — volume confirms uptrend",
                "Volume Accumulation", "OBV Trend")
        return None

    def _logic_vwap_bounce(self, df, symbol):
        """Price pulls back to VWAP and bounces with above-avg volume."""
        if len(df) < 25: return None
        df = self._compute_common(df)
        df["vwap20"] = vwap(df, window=20)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["vwap20"])
                and pv["close"] <= pv["vwap20"] * 1.005
                and lt["close"] > lt["vwap20"]
                and lt["close"] > lt["open"]
                and lt["vol_ratio"] > 1.2):
            return self._build_signal(symbol, df,
                f"Bounced from VWAP={round(lt['vwap20'],1)} with volume",
                "VWAP Bounce", "VWAP Strategy")
        return None

    def _logic_acc_dist(self, df, symbol):
        """A/D Line rising (positive slope) with price above SMA50."""
        if len(df) < 30: return None
        df = self._compute_common(df)
        df["ad"]    = accumulation_distribution(df)
        df["sma50"] = sma(df["close"], 50)
        lt = df.iloc[-1]
        # A/D line 10-day slope is positive
        ad_slope = df["ad"].iloc[-10:].diff().mean()
        if (pd.notna(ad_slope) and ad_slope > 0
                and lt["close"] > lt["sma50"]
                and lt["close"] > lt["open"]):
            return self._build_signal(symbol, df,
                f"A/D Line rising — smart money accumulating",
                "Accumulation Phase", "Acc/Distribution")
        return None

    # ══════════════════════════════════════════════════════════════════
    # NEW STRATEGIES — STOCHASTIC GROUP
    # ══════════════════════════════════════════════════════════════════

    def _logic_stoch_divergence(self, df, symbol):
        """Bullish Stochastic divergence: price lower low, Stoch %K higher low."""
        if len(df) < 35: return None
        df = self._compute_common(df)
        st = stochastic(df)
        df["stoch_k"] = st["k"]
        df["ema200"]  = ema(df["close"], 200)
        lt = df.iloc[-1]
        if (pd.isna(lt["stoch_k"]) or lt["close"] < lt["ema200"]):
            return None
        if self._bullish_divergence(df["close"].iloc[-25:], df["stoch_k"].iloc[-25:]):
            return self._build_signal(symbol, df,
                f"Stoch divergence: price ↓↓, %K ↑",
                "Stochastic Divergence", "Stoch Divergence")
        return None

    def _logic_stoch_trend(self, df, symbol):
        """Stochastic %K crosses %D in 20-50 range above EMA200 — not overbought."""
        if len(df) < 200: return None
        df = self._compute_common(df)
        st = stochastic(df)
        df["stoch_k"], df["stoch_d"] = st["k"], st["d"]
        df["ema200"] = ema(df["close"], 200)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["stoch_k"])
                and pv["stoch_k"] <= pv["stoch_d"]
                and lt["stoch_k"] > lt["stoch_d"]
                and 20 <= lt["stoch_k"] <= 60
                and lt["close"] > lt["ema200"]):
            return self._build_signal(symbol, df,
                f"Stoch %K={round(lt['stoch_k'],1)} crossed %D in trend zone",
                "Stochastic + Trend", "Stoch Trend")
        return None

    # ══════════════════════════════════════════════════════════════════
    # NEW STRATEGIES — FIBONACCI GROUP
    # ══════════════════════════════════════════════════════════════════

    def _fib_levels(self, df, lookback=60):
        """Compute Fibonacci retracement levels from recent swing high/low."""
        window = df.iloc[-lookback:]
        swing_high = float(window["high"].max())
        swing_low  = float(window["low"].min())
        diff = swing_high - swing_low
        return {
            "high":  swing_high,
            "low":   swing_low,
            "38.2":  swing_high - 0.382 * diff,
            "50.0":  swing_high - 0.500 * diff,
            "61.8":  swing_high - 0.618 * diff,
        }

    def _logic_fib_retracement(self, df, symbol):
        """Price at 38.2%–61.8% Fibonacci retracement of recent swing."""
        if len(df) < 65: return None
        df = self._compute_common(df)
        df["ema200"] = ema(df["close"], 200)
        lt = df.iloc[-1]
        if lt["close"] < lt["ema200"]:
            return None
        fibs = self._fib_levels(df, lookback=60)
        close = lt["close"]
        tolerance = 0.008  # 0.8% tolerance
        hit_level = None
        for level_name in ["61.8", "50.0", "38.2"]:
            fib_price = fibs[level_name]
            if abs(close - fib_price) / fib_price < tolerance:
                hit_level = level_name
                break
        if hit_level and 35 <= lt["rsi14"] <= 55:
            return self._build_signal(symbol, df,
                f"At Fib {hit_level}% ({round(fibs[hit_level],1)}), RSI={round(lt['rsi14'],1)}",
                "Fibonacci Support", "Fibonacci Retracement")
        return None

    def _logic_fib_trend(self, df, symbol):
        """Fibonacci 61.8% retracement + RSI 40-50 + above EMA200 — three-way confluence."""
        if len(df) < 65: return None
        df = self._compute_common(df)
        df["ema200"] = ema(df["close"], 200)
        lt = df.iloc[-1]
        if lt["close"] < lt["ema200"]:
            return None
        fibs = self._fib_levels(df, lookback=60)
        close = lt["close"]
        at_618 = abs(close - fibs["61.8"]) / fibs["61.8"] < 0.012
        rsi_ok = 38 <= lt["rsi14"] <= 52
        if at_618 and rsi_ok:
            return self._build_signal(symbol, df,
                f"Fib 61.8%={round(fibs['61.8'],1)}, RSI={round(lt['rsi14'],1)}, above EMA200",
                "Fib + RSI + Trend Confluence", "Fibonacci Trend")
        return None

    # ══════════════════════════════════════════════════════════════════
    # NEW STRATEGIES — ADX GROUP
    # ══════════════════════════════════════════════════════════════════

    def _logic_adx_breakout(self, df, symbol):
        """ADX rising above 20 + price breaks 20-day high — trend strengthening."""
        if len(df) < 25: return None
        df = self._compute_common(df)
        a = adx(df)
        df["adx14"]  = a["adx"]
        df["high20"] = df["close"].shift(1).rolling(20).max()
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["adx14"])
                and pv["adx14"] <= 20 and lt["adx14"] > 20
                and lt["close"] > lt["high20"]):
            return self._build_signal(symbol, df,
                f"ADX={round(lt['adx14'],1)} rising + price broke 20D high",
                "Trend + Breakout", "ADX Breakout")
        return None

    # ══════════════════════════════════════════════════════════════════
    # NEW STRATEGIES — ICHIMOKU GROUP
    # ══════════════════════════════════════════════════════════════════

    def _logic_ichimoku_cloud(self, df, symbol):
        """Price breaks above Ichimoku Cloud (both Senkou A & B)."""
        if len(df) < 80: return None
        df = self._compute_common(df)
        ic = ichimoku(df)
        df["ichi_a"] = ic["senkou_a"]
        df["ichi_b"] = ic["senkou_b"]
        lt, pv = df.iloc[-1], df.iloc[-2]
        cloud_top_now  = max(lt["ichi_a"], lt["ichi_b"]) if pd.notna(lt["ichi_a"]) else None
        cloud_top_prev = max(pv["ichi_a"], pv["ichi_b"]) if pd.notna(pv["ichi_a"]) else None
        if (cloud_top_now and cloud_top_prev
                and pv["close"] <= cloud_top_prev
                and lt["close"] > cloud_top_now):
            return self._build_signal(symbol, df,
                f"Price broke above Ichimoku cloud top={round(cloud_top_now,1)}",
                "Cloud Breakout", "Ichimoku Cloud")
        return None

    def _logic_tenkan_kijun_cross(self, df, symbol):
        """Tenkan-sen crosses above Kijun-sen (TK Cross) above the cloud."""
        if len(df) < 80: return None
        df = self._compute_common(df)
        ic = ichimoku(df)
        df["tenkan"] = ic["tenkan"]
        df["kijun"]  = ic["kijun"]
        df["ichi_a"] = ic["senkou_a"]
        df["ichi_b"] = ic["senkou_b"]
        lt, pv = df.iloc[-1], df.iloc[-2]
        if not all(pd.notna(v) for v in [lt["tenkan"], lt["kijun"], lt["ichi_a"], lt["ichi_b"]]):
            return None
        cloud_top = max(lt["ichi_a"], lt["ichi_b"])
        tk_cross  = pv["tenkan"] <= pv["kijun"] and lt["tenkan"] > lt["kijun"]
        above_cloud = lt["close"] > cloud_top
        if tk_cross and above_cloud:
            return self._build_signal(symbol, df,
                f"TK Cross: Tenkan({round(lt['tenkan'],1)}) > Kijun({round(lt['kijun'],1)}) above cloud",
                "TK Cross Signal", "Tenkan-Kijun Cross")
        return None

    def _logic_kumo_twist(self, df, symbol):
        """Senkou A crosses above Senkou B — Kumo turns bullish."""
        if len(df) < 90: return None
        df = self._compute_common(df)
        ic = ichimoku(df)
        df["ichi_a"] = ic["senkou_a"]
        df["ichi_b"] = ic["senkou_b"]
        # Kumo is projected 26 bars ahead; compare recent
        valid = df.dropna(subset=["ichi_a", "ichi_b"])
        if len(valid) < 3:
            return None
        lt, pv = valid.iloc[-1], valid.iloc[-2]
        kumo_twist = pv["ichi_a"] <= pv["ichi_b"] and lt["ichi_a"] > lt["ichi_b"]
        if kumo_twist and df.iloc[-1]["close"] > lt["ichi_a"]:
            return self._build_signal(symbol, df,
                f"Kumo twist: Senkou A={round(lt['ichi_a'],1)} crossed above B={round(lt['ichi_b'],1)}",
                "Cloud Turning Bullish", "Kumo Twist")
        return None

    # ══════════════════════════════════════════════════════════════════
    # NEW STRATEGIES — SUPERTREND GROUP
    # ══════════════════════════════════════════════════════════════════

    def _logic_supertrend_ema(self, df, symbol):
        """Supertrend bullish + EMA50 rising + price above EMA200."""
        if len(df) < 200: return None
        df = self._compute_common(df)
        st = supertrend(df)
        df["st_trend"] = st["trend"]
        df["ema50"]    = ema(df["close"], 50)
        df["ema200"]   = ema(df["close"], 200)
        lt, pv = df.iloc[-1], df.iloc[-2]
        ema50_rising = lt["ema50"] > pv["ema50"]
        if (lt["st_trend"] == 1
                and ema50_rising
                and lt["close"] > lt["ema200"]):
            return self._build_signal(symbol, df,
                f"Supertrend bullish + EMA50 rising + above EMA200",
                "Triple Alignment Bullish", "Supertrend + EMA")
        return None

    def _logic_supertrend_volume(self, df, symbol):
        """Supertrend bullish flip with volume > 1.5x — confirmed reversal."""
        if len(df) < 15: return None
        df = self._compute_common(df)
        st = supertrend(df)
        df["st_trend"] = st["trend"]
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pv["st_trend"] == -1 and lt["st_trend"] == 1
                and lt["vol_ratio"] > 1.5):
            return self._build_signal(symbol, df,
                f"Supertrend flipped bullish + Vol={round(lt['vol_ratio'],1)}x",
                "Volume-Confirmed Reversal", "Supertrend + Volume")
        return None

    def _logic_supertrend_adx(self, df, symbol):
        """Supertrend bullish + ADX > 25 — strong trending move confirmed."""
        if len(df) < 25: return None
        df = self._compute_common(df)
        st = supertrend(df)
        a  = adx(df)
        df["st_trend"]  = st["trend"]
        df["adx14"]    = a["adx"]
        df["plus_di"]  = a["plus_di"]
        df["minus_di"] = a["minus_di"]
        lt = df.iloc[-1]
        if (lt["st_trend"] == 1
                and pd.notna(lt["adx14"]) and lt["adx14"] > 25
                and lt["plus_di"] > lt["minus_di"]):
            return self._build_signal(symbol, df,
                f"Supertrend bullish + ADX={round(lt['adx14'],1)} strong trend",
                "Trend Strength Confirmed", "Supertrend + ADX")
        return None

    # ══════════════════════════════════════════════════════════════════
    # NEW STRATEGIES — ADVANCED INDICATORS
    # ══════════════════════════════════════════════════════════════════

    def _logic_parabolic_sar(self, df, symbol):
        """Parabolic SAR flips from above to below price — bullish trend reversal."""
        if len(df) < 10: return None
        df = self._compute_common(df)
        df["psar"] = parabolic_sar(df)
        lt, pv = df.iloc[-1], df.iloc[-2]
        df["sma50"] = sma(df["close"], 50)
        lt = df.iloc[-1]
        # SAR was above price (bearish), now below price (bullish)
        sar_flipped = pv["psar"] > pv["close"] and lt["psar"] < lt["close"]
        if sar_flipped and lt["close"] > df["sma50"].iloc[-1]:
            return self._build_signal(symbol, df,
                f"SAR={round(lt['psar'],1)} flipped below price={round(lt['close'],1)}",
                "SAR Bullish Flip", "Parabolic SAR")
        return None

    def _logic_donchian_breakout(self, df, symbol):
        """Price breaks above Donchian upper channel with volume surge."""
        if len(df) < 25: return None
        df = self._compute_common(df)
        dc = donchian_channel(df, period=20)
        df["dc_upper"] = dc["upper"].shift(1)  # Previous session's upper
        lt = df.iloc[-1]
        if (pd.notna(lt["dc_upper"])
                and lt["close"] > lt["dc_upper"]
                and lt["vol_ratio"] > 1.5):
            return self._build_signal(symbol, df,
                f"Broke Donchian upper={round(lt['dc_upper'],1)} with Vol={round(lt['vol_ratio'],1)}x",
                "Donchian Breakout", "Donchian Channel")
        return None

    def _logic_williams_r_bounce(self, df, symbol):
        """Williams %R crosses up from below -80 while above SMA50."""
        if len(df) < 50: return None
        df = self._compute_common(df)
        df["wr"]    = williams_r(df, period=14)
        df["sma50"] = sma(df["close"], 50)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if (pd.notna(lt["wr"])
                and pv["wr"] <= -80 and lt["wr"] > -80
                and lt["close"] > lt["sma50"]):
            return self._build_signal(symbol, df,
                f"Williams %R={round(lt['wr'],1)} bounced from oversold",
                "Williams %R Reversal", "Williams %R Bounce")
        return None

    def _logic_pivot_bounce(self, df, symbol):
        """Price bouncing from S1/S2 pivot support with RSI < 45."""
        if len(df) < 5: return None
        df = self._compute_common(df)
        pp = pivot_points(df)
        df["s1"], df["s2"] = pp["s1"], pp["s2"]
        lt = df.iloc[-1]
        tolerance = 0.01  # 1% tolerance
        at_s1 = pd.notna(lt["s1"]) and abs(lt["close"] - lt["s1"]) / lt["s1"] < tolerance
        at_s2 = pd.notna(lt["s2"]) and abs(lt["close"] - lt["s2"]) / lt["s2"] < tolerance
        if (at_s1 or at_s2) and lt["rsi14"] < 45 and lt["close"] > lt["open"]:
            level = "S1" if at_s1 else "S2"
            lvl_price = lt["s1"] if at_s1 else lt["s2"]
            return self._build_signal(symbol, df,
                f"Bouncing from {level} pivot={round(lvl_price,1)}, RSI={round(lt['rsi14'],1)}",
                "Pivot Support Bounce", "Pivot Points")
        return None

    # ══════════════════════════════════════════════════════════════════
    # NEW STRATEGIES — MULTI-INDICATOR COMBOS
    # ══════════════════════════════════════════════════════════════════

    def _logic_rsi_macd(self, df, symbol):
        """RSI > 50 AND MACD crossover both aligned bullish — dual momentum."""
        if len(df) < 35: return None
        df = self._compute_common(df)
        m = macd(df["close"])
        df["macd_l"], df["macd_s"] = m["macd"], m["signal"]
        lt, pv = df.iloc[-1], df.iloc[-2]
        macd_cross = pv["macd_l"] <= pv["macd_s"] and lt["macd_l"] > lt["macd_s"]
        rsi_bullish = lt["rsi14"] > 50
        if macd_cross and rsi_bullish:
            return self._build_signal(symbol, df,
                f"RSI={round(lt['rsi14'],1)} > 50 + MACD bullish cross",
                "Dual Momentum Signal", "RSI + MACD")
        return None

    def _logic_ema_rsi_volume(self, df, symbol):
        """EMA200 uptrend + RSI 40-60 pullback reset + volume spike."""
        if len(df) < 200: return None
        df = self._compute_common(df)
        df["ema200"] = ema(df["close"], 200)
        lt = df.iloc[-1]
        rsi_reset  = 38 <= lt["rsi14"] <= 58
        above_ema  = lt["close"] > lt["ema200"]
        vol_spike  = lt["vol_ratio"] > 1.5
        if rsi_reset and above_ema and vol_spike:
            return self._build_signal(symbol, df,
                f"EMA200 uptrend + RSI={round(lt['rsi14'],1)} reset + Vol={round(lt['vol_ratio'],1)}x",
                "Institutional Accumulation", "EMA + RSI + Volume")
        return None

    def _logic_bb_rsi(self, df, symbol):
        """Lower BB touch + RSI < 40 + above SMA200 — mean reversion combo."""
        if len(df) < 200: return None
        df = self._compute_common(df)
        bb = bollinger_bands(df["close"])
        df["bb_lower"] = bb["lower"]
        df["sma200"]   = sma(df["close"], 200)
        lt = df.iloc[-1]
        if (pd.notna(lt["bb_lower"])
                and lt["close"] <= lt["bb_lower"] * 1.01
                and lt["rsi14"] < 40
                and lt["close"] > lt["sma200"]):
            return self._build_signal(symbol, df,
                f"BB lower touch + RSI={round(lt['rsi14'],1)} + above SMA200",
                "BB + RSI Mean Reversion", "Bollinger + RSI")
        return None

    def _logic_vwap_price_action(self, df, symbol):
        """Price above VWAP + bullish engulfing or hammer candle."""
        if len(df) < 25: return None
        df = self._compute_common(df)
        df["vwap20"] = vwap(df, window=20)
        lt, pv = df.iloc[-1], df.iloc[-2]
        if not pd.notna(lt["vwap20"]) or lt["close"] < lt["vwap20"]:
            return None
        # Check for hammer
        body   = abs(lt["close"] - lt["open"])
        rng    = lt["high"] - lt["low"]
        lower_wick = min(lt["close"], lt["open"]) - lt["low"]
        is_hammer = rng > 0 and lower_wick >= 2 * body
        # Check for engulfing
        is_engulfing = (pv["close"] < pv["open"] and lt["close"] > lt["open"]
                        and lt["close"] > pv["open"] and lt["open"] < pv["close"])
        if (is_hammer or is_engulfing) and lt["vol_ratio"] > 1.2:
            pattern = "Hammer" if is_hammer else "Engulfing"
            return self._build_signal(symbol, df,
                f"{pattern} above VWAP={round(lt['vwap20'],1)}, Vol={round(lt['vol_ratio'],1)}x",
                "VWAP + Price Action", "VWAP + PA")
        return None

    def _logic_supertrend_adx_vol(self, df, symbol):
        """Supertrend bullish + ADX > 25 + volume > 1.5x — full-strength signal."""
        if len(df) < 25: return None
        df = self._compute_common(df)
        st = supertrend(df)
        a  = adx(df)
        df["st_trend"] = st["trend"]
        df["adx14"]    = a["adx"]
        lt = df.iloc[-1]
        if (lt["st_trend"] == 1
                and pd.notna(lt["adx14"]) and lt["adx14"] > 25
                and lt["vol_ratio"] > 1.5):
            return self._build_signal(symbol, df,
                f"Supertrend + ADX={round(lt['adx14'],1)} + Vol={round(lt['vol_ratio'],1)}x",
                "High-Conviction Signal", "Supertrend + ADX + Vol")
        return None

    def _logic_rsi_fibonacci(self, df, symbol):
        """Price at Fibonacci 61.8% + RSI 35-50 bounce — precision confluence."""
        if len(df) < 70: return None
        df = self._compute_common(df)
        df["ema200"] = ema(df["close"], 200)
        lt = df.iloc[-1]
        if lt["close"] < lt["ema200"]:
            return None
        fibs = self._fib_levels(df, lookback=60)
        at_618 = abs(lt["close"] - fibs["61.8"]) / fibs["61.8"] < 0.015
        rsi_ok = 35 <= lt["rsi14"] <= 52
        if at_618 and rsi_ok:
            return self._build_signal(symbol, df,
                f"Fib 61.8%={round(fibs['61.8'],1)}, RSI={round(lt['rsi14'],1)}, EMA200 uptrend",
                "RSI + Fibonacci Confluence", "RSI + Fibonacci")
        return None

    def _logic_ichimoku_volume(self, df, symbol):
        """Price above Ichimoku Cloud + volume > 1.5x — cloud breakout confirmed."""
        if len(df) < 80: return None
        df = self._compute_common(df)
        ic = ichimoku(df)
        df["ichi_a"] = ic["senkou_a"]
        df["ichi_b"] = ic["senkou_b"]
        lt = df.iloc[-1]
        if not pd.notna(lt["ichi_a"]):
            return None
        cloud_top = max(lt["ichi_a"], lt["ichi_b"])
        if lt["close"] > cloud_top and lt["vol_ratio"] > 1.5:
            return self._build_signal(symbol, df,
                f"Above cloud top={round(cloud_top,1)}, Vol={round(lt['vol_ratio'],1)}x",
                "Ichimoku + Volume", "Ichimoku + Volume")
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    manager = StrategyManager()
    for strat_id in STRATEGY_DESCRIPTIONS:
        try:
            sigs = manager.get_signals(strat_id)
            print(f"{strat_id:30s} → {len(sigs):3d} signal(s)")
        except Exception as e:
            print(f"{strat_id:30s} → ERROR: {e}")
