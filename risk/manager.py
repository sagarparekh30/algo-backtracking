"""
Risk management for position sizing and trade validation.
"""

import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import load_env  # noqa: F401 — auto-loads .env

from config.settings import (
    INITIAL_CAPITAL,
    RISK_PER_TRADE_PCT,
    MAX_OPEN_POSITIONS,
    ATR_MULTIPLIER,
    REWARD_RISK_RATIO,
)

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Handles position sizing, stop-loss and target calculation,
    and portfolio-level risk checks.
    """

    def __init__(
        self,
        capital: float = None,
        risk_pct: float = None,
        max_positions: int = None,
        atr_multiplier: float = None,
        reward_risk_ratio: float = None,
    ):
        """
        Args:
            capital: Total trading capital in base currency.
            risk_pct: Maximum risk per trade as a percentage of capital (e.g., 1.0 = 1%).
            max_positions: Maximum number of simultaneous open positions.
            atr_multiplier: Multiplier applied to ATR for stop-loss distance.
            reward_risk_ratio: Target distance as a multiple of risk distance (e.g., 2.0).
        """
        self.capital = capital if capital is not None else INITIAL_CAPITAL
        self.risk_pct = risk_pct if risk_pct is not None else RISK_PER_TRADE_PCT
        self.max_positions = max_positions if max_positions is not None else MAX_OPEN_POSITIONS
        self.atr_multiplier = atr_multiplier if atr_multiplier is not None else ATR_MULTIPLIER
        self.reward_risk_ratio = reward_risk_ratio if reward_risk_ratio is not None else REWARD_RISK_RATIO

    # ------------------------------------------------------------------
    # Core calculations
    # ------------------------------------------------------------------

    def calculate_position_size(
        self,
        entry_price: float,
        stop_loss_price: float,
        capital: float = None,
    ) -> int:
        """
        Calculate the number of shares to buy based on risk per trade.

        Risk per trade = capital * risk_pct / 100
        Shares = risk_amount / (entry_price - stop_loss_price)

        Args:
            entry_price: Planned entry price.
            stop_loss_price: Stop-loss price.
            capital: Capital to use (defaults to self.capital).

        Returns:
            Number of shares (integer, minimum 0).
        """
        try:
            cap = capital if capital is not None else self.capital
            risk_amount = cap * (self.risk_pct / 100.0)
            risk_per_share = abs(entry_price - stop_loss_price)

            if risk_per_share <= 0:
                logger.warning("Risk per share is zero or negative — returning 0 shares.")
                return 0

            shares = int(risk_amount / risk_per_share)
            return max(0, shares)
        except Exception as e:
            logger.error(f"calculate_position_size error: {e}")
            return 0

    def calculate_stop_loss(
        self,
        entry_price: float,
        atr_value: float,
        side: str = "long",
    ) -> float:
        """
        Calculate stop-loss price using ATR.

        Args:
            entry_price: Entry price.
            atr_value: ATR value at entry.
            side: 'long' or 'short'.

        Returns:
            Stop-loss price.
        """
        try:
            distance = self.atr_multiplier * atr_value
            if side == "long":
                return round(entry_price - distance, 2)
            else:
                return round(entry_price + distance, 2)
        except Exception as e:
            logger.error(f"calculate_stop_loss error: {e}")
            return entry_price

    def calculate_target(
        self,
        entry_price: float,
        stop_loss_price: float,
        side: str = "long",
    ) -> float:
        """
        Calculate target price based on reward:risk ratio.

        Args:
            entry_price: Entry price.
            stop_loss_price: Stop-loss price.
            side: 'long' or 'short'.

        Returns:
            Target price.
        """
        try:
            risk_distance = abs(entry_price - stop_loss_price)
            reward_distance = risk_distance * self.reward_risk_ratio
            if side == "long":
                return round(entry_price + reward_distance, 2)
            else:
                return round(entry_price - reward_distance, 2)
        except Exception as e:
            logger.error(f"calculate_target error: {e}")
            return entry_price

    def get_trade_params(
        self,
        entry_price: float,
        atr_value: float,
        available_capital: float = None,
        side: str = "long",
    ) -> dict:
        """
        Compute all trade parameters in one call.

        Args:
            entry_price: Planned entry price.
            atr_value: ATR value at signal bar.
            available_capital: Available capital (defaults to self.capital).
            side: 'long' or 'short'.

        Returns:
            dict with keys:
                stop_loss, target, shares, risk_amount, position_value
        """
        try:
            cap = available_capital if available_capital is not None else self.capital
            stop_loss = self.calculate_stop_loss(entry_price, atr_value, side)
            target = self.calculate_target(entry_price, stop_loss, side)
            shares = self.calculate_position_size(entry_price, stop_loss, cap)
            risk_per_share = abs(entry_price - stop_loss)
            risk_amount = shares * risk_per_share
            position_value = shares * entry_price

            return {
                "stop_loss": stop_loss,
                "target": target,
                "shares": shares,
                "risk_amount": round(risk_amount, 2),
                "position_value": round(position_value, 2),
            }
        except Exception as e:
            logger.error(f"get_trade_params error: {e}")
            return {
                "stop_loss": entry_price,
                "target": entry_price,
                "shares": 0,
                "risk_amount": 0.0,
                "position_value": 0.0,
            }

    def is_portfolio_safe(self, current_open_positions_count: int) -> bool:
        """
        Check whether it is safe to add another position.

        Args:
            current_open_positions_count: Number of currently open positions.

        Returns:
            True if adding one more position is within limits.
        """
        return current_open_positions_count < self.max_positions
