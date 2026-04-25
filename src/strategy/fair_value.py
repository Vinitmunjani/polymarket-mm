"""
Black-Scholes fair value for binary crypto options.

Computes P(price > strike at expiry) using the digital option formula.
This is the core edge — updates every 2s from CEX price data,
far faster than Polymarket's order book mid.
"""

import math
import time
from scipy.stats import norm

from src.monitoring.logger import get_logger

log = get_logger("fair_value")


class CryptoBinaryFairValue:
    """
    Fair value for "Will ASSET be above STRIKE at EXPIRY?"
    using Black-Scholes digital option pricing.

    No risk-free rate term — irrelevant for 15-minute durations.
    """

    def __init__(self, strike: float, expiry_ts: float,
                 open_ts: float = None):
        """
        Args:
            strike: Strike price (e.g., 94500.0 for BTC).
            expiry_ts: Unix timestamp of market resolution.
            open_ts: Unix timestamp of market open (for T normalization).
        """
        self.strike = strike
        self.expiry_ts = expiry_ts
        self.open_ts = open_ts or (expiry_ts - 900)  # Default 15 min
        self._last_fair_value = 0.50
        self._last_update_ts = 0.0

    def fair_value(self, current_price: float,
                   sigma_annualized: float,
                   now_ts: float = None) -> float:
        """
        Compute P(price > strike at expiry).

        Args:
            current_price: Live spot price from CEX (Binance).
            sigma_annualized: Annualized volatility (e.g., 0.60 = 60%).
            now_ts: Current timestamp (defaults to time.time()).

        Returns:
            Probability in [0.01, 0.99].
        """
        now_ts = now_ts or time.time()

        # Time to expiry in years
        t_seconds = max(1, self.expiry_ts - now_ts)
        t_years = t_seconds / (365.25 * 86400)

        if current_price <= 0 or self.strike <= 0:
            log.warning("invalid_price_or_strike",
                       price=current_price, strike=self.strike)
            return 0.50

        if sigma_annualized <= 0:
            # Zero vol: deterministic outcome
            return 0.99 if current_price > self.strike else 0.01

        try:
            # d2 from Black-Scholes (no risk-free rate for short durations)
            log_ratio = math.log(current_price / self.strike)
            vol_sqrt_t = sigma_annualized * math.sqrt(t_years)

            d2 = (log_ratio - 0.5 * sigma_annualized ** 2 * t_years) / vol_sqrt_t

            prob = norm.cdf(d2)

            # Clamp to valid range
            prob = max(0.01, min(0.99, prob))

            self._last_fair_value = prob
            self._last_update_ts = now_ts

            return prob

        except (ValueError, ZeroDivisionError) as e:
            log.error("fair_value_calc_error", error=str(e),
                     price=current_price, strike=self.strike, sigma=sigma_annualized)
            return self._last_fair_value

    def time_remaining_seconds(self, now_ts: float = None) -> float:
        """Seconds until market resolution."""
        now_ts = now_ts or time.time()
        return max(0, self.expiry_ts - now_ts)

    def normalized_time(self, now_ts: float = None) -> float:
        """
        T normalized to [0, 1]. 1 = just opened, 0 = at resolution.
        """
        now_ts = now_ts or time.time()
        total = self.expiry_ts - self.open_ts
        if total <= 0:
            return 0.0
        remaining = self.expiry_ts - now_ts
        return max(0.0, min(1.0, remaining / total))

    @property
    def last_fair_value(self) -> float:
        """Most recently computed fair value."""
        return self._last_fair_value

    @property
    def is_stale(self) -> bool:
        """True if fair value hasn't been updated in > 5 seconds."""
        return (time.time() - self._last_update_ts) > 5.0
