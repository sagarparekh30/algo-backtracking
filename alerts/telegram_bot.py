"""
Telegram alert notifications for trading signals and pipeline status.

All methods return False gracefully if the bot token is not configured.
"""

import os
import sys
import logging
from typing import Optional

import urllib.request
import urllib.parse
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


class TelegramAlert:
    """
    Send Telegram messages via the Bot API.

    If TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are not set, all methods
    return False without raising exceptions.
    """

    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str = None, chat_id: str = None):
        self.bot_token = bot_token or TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or TELEGRAM_CHAT_ID

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_configured(self) -> bool:
        if not self.bot_token or not self.chat_id:
            logger.debug("Telegram not configured — skipping notification.")
            return False
        return True

    def _post(self, text: str) -> bool:
        """Send a raw text message via Telegram Bot API."""
        if not self._is_configured():
            return False

        url = self.BASE_URL.format(token=self.bot_token)
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))
                if result.get("ok"):
                    return True
                else:
                    logger.warning(f"Telegram API error: {result.get('description')}")
                    return False
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_message(self, text: str) -> bool:
        """
        Send a plain message to the configured chat.

        Args:
            text: Message text (supports HTML formatting).

        Returns:
            True if sent successfully, False otherwise.
        """
        return self._post(text)

    def send_signal(self, signal: dict) -> bool:
        """
        Send a formatted trading signal notification.

        Args:
            signal: Signal dict with keys: symbol, strategy, price,
                    stop_loss, target, rsi, volume_ratio, atr.

        Returns:
            True if sent successfully, False otherwise.
        """
        try:
            symbol = signal.get("symbol", "N/A")
            strategy = signal.get("strategy", "N/A")
            price = signal.get("price", 0.0)
            stop_loss = signal.get("stop_loss", 0.0)
            target = signal.get("target", 0.0)
            rsi_val = signal.get("rsi")
            vol_ratio = signal.get("volume_ratio")
            atr_val = signal.get("atr")
            trend = signal.get("trend", "")

            # Calculate risk:reward
            risk = abs(price - stop_loss) if stop_loss else 0
            reward = abs(target - price) if target else 0
            rr = f"{round(reward / risk, 1)}" if risk > 0 else "N/A"

            lines = [
                f"<b>📈 SIGNAL: {symbol}</b>",
                f"Strategy : {strategy}",
                f"Trend    : {trend}",
                f"",
                f"Entry    : ₹{price:,.2f}",
                f"Stop Loss: ₹{stop_loss:,.2f}",
                f"Target   : ₹{target:,.2f}",
                f"R:R      : 1:{rr}",
            ]

            if rsi_val is not None:
                lines.append(f"RSI      : {rsi_val}")
            if vol_ratio is not None:
                lines.append(f"Vol Ratio: {vol_ratio}x")
            if atr_val is not None:
                lines.append(f"ATR      : ₹{atr_val:.2f}")

            text = "\n".join(lines)
            return self._post(text)
        except Exception as e:
            logger.error(f"send_signal error: {e}")
            return False

    def send_morning_report(
        self,
        signals: list,
        backfill_status: Optional[dict] = None,
    ) -> bool:
        """
        Send a daily morning digest with all signals found.

        Args:
            signals: List of signal dicts (combined from all strategies).
            backfill_status: Optional dict with backfill stats
                             (e.g. {'symbols': 100, 'candles': 500}).

        Returns:
            True if sent successfully, False otherwise.
        """
        try:
            from datetime import date
            today = date.today().strftime("%d %b %Y")

            lines = [
                f"<b>🌅 Morning Report — {today}</b>",
                "",
            ]

            if backfill_status:
                syms = backfill_status.get("symbols", "N/A")
                candles = backfill_status.get("candles", "N/A")
                lines += [
                    f"<b>Data Backfill</b>",
                    f"Symbols processed : {syms}",
                    f"New candles       : {candles}",
                    "",
                ]

            if signals:
                lines.append(f"<b>Signals Found: {len(signals)}</b>")
                # Group by strategy
                by_strategy: dict = {}
                for sig in signals:
                    strat = sig.get("strategy", "Unknown")
                    by_strategy.setdefault(strat, []).append(sig)

                for strat, sigs in by_strategy.items():
                    lines.append(f"\n<u>{strat}</u> ({len(sigs)})")
                    for s in sigs:
                        lines.append(
                            f"  {s['symbol']} — ₹{s['price']:,.0f}"
                            f" | SL ₹{s.get('stop_loss', 0):,.0f}"
                            f" | T ₹{s.get('target', 0):,.0f}"
                        )
            else:
                lines.append("No signals found today.")

            text = "\n".join(lines)
            return self._post(text)
        except Exception as e:
            logger.error(f"send_morning_report error: {e}")
            return False

    def send_execute_alert(self, results: list, regime: str, threshold_pct: int) -> bool:
        """
        Send AI-confirmed Execute tab trades via Telegram after daily update.

        Args:
            results:       List of combined signal dicts from /api/combined/signals
            regime:        Market regime string ("Bull" / "Neutral" / "Bear")
            threshold_pct: Buy threshold percentage used (55 / 60 / 65)

        Returns:
            True if sent successfully, False otherwise.
        """
        if not results:
            return False
        try:
            from datetime import date
            today = date.today().strftime("%d %b %Y")
            regime_emoji = {"Bull": "🟢", "Neutral": "🟡", "Bear": "🔴"}.get(regime, "⚪")

            lines = [
                f"<b>⚡ Execute Alert — {today}</b>",
                f"{regime_emoji} {regime} Market · AI threshold {threshold_pct}%",
                f"<b>{len(results)} high-conviction trade{'s' if len(results)>1 else ''} found</b>",
                "",
            ]

            for i, r in enumerate(results[:5], 1):   # top 5 max
                sym   = r.get("symbol", "")
                prob  = int(round(r.get("buy_probability", 0) * 100))
                conf  = r.get("confidence", "")
                entry = r.get("entry") or r.get("price", 0)
                sl    = r.get("stop_loss", 0)
                tgt   = r.get("target") or r.get("price_target", 0)
                exp   = r.get("expected_return_pct")
                strats = ", ".join(r.get("strategies", []))
                sc    = r.get("strategy_count", 1)

                lines.append(f"<b>#{i} {sym}</b>  {prob}% · {conf}")
                lines.append(f"  Strategies : {strats} ({sc})")
                lines.append(f"  Entry ₹{entry:,.0f}  SL ₹{sl:,.0f}  T ₹{tgt:,.0f}")
                if exp is not None:
                    lines.append(f"  AI target  : +{exp}%")
                lines.append("")

            if len(results) > 5:
                lines.append(f"… and {len(results)-5} more on the dashboard")

            return self._post("\n".join(lines))
        except Exception as e:
            logger.error(f"send_execute_alert error: {e}")
            return False

    def send_portfolio_heat(self, heat_pct: float, sector_exposure: dict,
                             open_count: int = 0) -> bool:
        """
        Daily 9am IST portfolio heat summary.

        Args:
            heat_pct:         Total capital at risk as % of capital.
            sector_exposure:  {sector: exposure_pct} from PortfolioHeatChecker.
            open_count:       Number of currently open positions.
        """
        try:
            from datetime import date
            today   = date.today().strftime("%d %b %Y")
            heat_emoji = "🟢" if heat_pct < 3 else ("🟡" if heat_pct < 5 else "🔴")
            lines   = [
                f"<b>🌡 Portfolio Heat Update — {today}</b>",
                f"{heat_emoji} Total risk: <b>{heat_pct:.1f}%</b> of capital",
                f"Open positions: {open_count}",
                "",
            ]
            if sector_exposure:
                lines.append("<b>Sector Exposure</b>")
                for sec, pct in sorted(sector_exposure.items(), key=lambda x: -x[1]):
                    bar = "█" * int(pct / 5)
                    lines.append(f"  {sec[:12]:<12}  {bar} {pct:.1f}%")
            return self._post("\n".join(lines))
        except Exception as e:
            logger.error(f"send_portfolio_heat error: {e}")
            return False

    def send_new_signals(self, results: list, regime: str = "Neutral") -> bool:
        """
        Post-market signal alert with quality score and top 2 strategies.
        Identical to send_execute_alert but emphasises quality score and
        position sizing.
        """
        if not results:
            return False
        try:
            from datetime import date
            today = date.today().strftime("%d %b %Y")
            regime_emoji = {"Bull": "🟢", "Neutral": "🟡", "Bear": "🔴"}.get(regime, "⚪")

            lines = [
                f"<b>📊 New Trade Signals — {today}</b>",
                f"{regime_emoji} {regime} Market · {len(results)} confirmed",
                "",
            ]

            for i, r in enumerate(results[:5], 1):
                sym   = r.get("symbol", "")
                prob  = int(round(r.get("buy_probability", 0) * 100))
                qs    = r.get("quality_score", 0)
                entry = r.get("entry") or r.get("price", 0)
                sl    = r.get("stop_loss", 0)
                tgt   = r.get("target") or r.get("price_target", 0)
                strats = r.get("strategies", [])[:2]
                ps    = r.get("position_size") or {}
                shares = ps.get("shares", "—")
                risk_inr = ps.get("risk_inr", 0)
                ev_status = (r.get("event_risk") or {}).get("status", "")
                ev_warn = " ⚠️ EVENT" if ev_status == "BLOCK" else (" 📅" if ev_status == "CAUTION" else "")

                lines.append(f"<b>#{i} {sym}</b>  AI {prob}%  QS {qs}/100{ev_warn}")
                lines.append(f"  Strategies : {', '.join(strats)}")
                lines.append(f"  Entry ₹{entry:,.0f} · SL ₹{sl:,.0f} · T ₹{tgt:,.0f}")
                if shares != "—":
                    lines.append(f"  Size: {shares} shares · Risk ₹{risk_inr:,.0f}")
                lines.append("")

            if len(results) > 5:
                lines.append(f"… +{len(results)-5} more on the dashboard")

            return self._post("\n".join(lines))
        except Exception as e:
            logger.error(f"send_new_signals error: {e}")
            return False

    def send_backfill_complete(self, stats: dict) -> bool:
        """
        Send a notification when the data backfill is complete.

        Args:
            stats: dict with keys such as: total_symbols, updated,
                   up_to_date, failed, total_candles.

        Returns:
            True if sent successfully, False otherwise.
        """
        try:
            lines = [
                "<b>✅ Backfill Complete</b>",
                "",
                f"Total symbols : {stats.get('total_symbols', 'N/A')}",
                f"Updated       : {stats.get('updated', 'N/A')}",
                f"Up to date    : {stats.get('up_to_date', 'N/A')}",
                f"Failed        : {stats.get('failed', 0)}",
                f"New candles   : {stats.get('total_candles', 'N/A')}",
            ]
            text = "\n".join(lines)
            return self._post(text)
        except Exception as e:
            logger.error(f"send_backfill_complete error: {e}")
            return False
