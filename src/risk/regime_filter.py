"""
Market regime filter — detects STABLE, TRENDING, and SPIKE conditions.

Thresholds are calibrated for P(Up) fair values that naturally move
±5-10% as the volatility estimator stabilizes in the first 30 seconds.

STABLE  → normal quoting
TRENDING → widen spreads 2x
SPIKE   → pause quoting until stable
"""

from collections import deque
from src.monitoring.logger import get_logger

log = get_logger("regime_filter")


class RegimeFilter:
    STABLE = "STABLE"
    TRENDING = "TRENDING"
    SPIKE = "SPIKE"

    def __init__(self, lookback=30, trend_threshold=0.08, spike_threshold=0.20):
        """
        Args:
            lookback: Number of fair value observations to track.
            trend_threshold: Drift over window to trigger TRENDING.
                             0.08 = 8% drift in P(Up) over 30 ticks (~60s).
            spike_threshold: Single-tick move to trigger SPIKE.
                             0.20 = 20% sudden move in P(Up).
                             (Was 0.15 but false-triggered during vol warmup)
        """
        self.mids = deque(maxlen=lookback)
        self.trend_threshold = trend_threshold
        self.spike_threshold = spike_threshold
        self._warmup_ticks = 0
        self._warmup_required = 120  # 120 ticks @ 4Hz = 30s (matches vol estimator)

    def update(self, mid: float):
        self.mids.append(mid)
        self._warmup_ticks += 1

    def regime(self) -> str:
        # Don't make regime calls during warmup
        if self._warmup_ticks < self._warmup_required or len(self.mids) < 5:
            return self.STABLE

        mids = list(self.mids)

        # Single-tick spike detection
        if len(mids) >= 2:
            last_move = abs(mids[-1] - mids[-2])
            if last_move > self.spike_threshold:
                return self.SPIKE

        # Drift over window
        if len(mids) >= 10:
            drift = abs(mids[-1] - mids[-10])
            if drift > self.trend_threshold:
                return self.TRENDING

        return self.STABLE

    def is_safe_to_quote(self) -> tuple[bool, float | None]:
        """Returns (should_quote, spread_multiplier_override)."""
        r = self.regime()
        if r == self.SPIKE:
            log.warning("regime_spike", msg="Pausing quotes — price spike detected")
            return False, None
        if r == self.TRENDING:
            return True, 2.0
        return True, None
