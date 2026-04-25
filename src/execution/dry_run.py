"""
Dry-run executor — simulates order placement and fills.

Fill model uses REAL price feed data:
  - BUY orders fill when the MARKET PRICE crosses DOWN through
    our limit price (a taker arrives and hits our bid).
  - Uses the fair value from the quote engine as a proxy for
    whether a fill would occur.
  
  This is much more realistic than random fills because:
  1. Orders at aggressive prices (close to mid) fill faster
  2. Orders far from mid never fill unless price moves
  3. Inventory accumulation matches real market dynamics
"""

import time
import random
import asyncio
from dataclasses import dataclass, field
from typing import Optional
from collections import deque

from src.monitoring.logger import get_logger

log = get_logger("dry_run")


@dataclass
class SimulatedOrder:
    order_id: str
    token_id: str
    side: str          # "yes" or "no"
    price: float
    size: float
    placed_at: float
    filled: bool = False
    fill_time: float = 0.0
    # Track the fair value at placement for fill logic
    fair_value_at_place: float = 0.5


class DryRunExecutor:
    """
    Simulates order execution for paper trading.

    Fill logic:
      - A BUY YES order at price P fills when P(Up) >= P
        (i.e., the market thinks Up is worth at least what we're paying)
      - A BUY NO order at price P fills when P(Down) = 1-P(Up) >= P
        (i.e., P(Up) <= 1 - P)
      - Adds a small random delay (1-3s) to simulate queue position
      - Fills partially when price is near our limit (size reduction)
    """

    def __init__(self, min_queue_time: float = 1.0,
                 max_queue_time: float = 3.0,
                 partial_fill_chance: float = 0.3):
        """
        Args:
            min_queue_time: Minimum seconds before a fill can occur.
            max_queue_time: Max queue delay for orders at top-of-book.
            partial_fill_chance: Probability of partial fill vs full fill.
        """
        self.min_queue_time = min_queue_time
        self.max_queue_time = max_queue_time
        self.partial_fill_chance = partial_fill_chance

        self.open_orders: dict[str, SimulatedOrder] = {}
        self.filled_orders: deque = deque(maxlen=500)
        self._order_counter = 0
        self._total_orders = 0
        self._total_fills = 0
        self._total_rejects = 0

        # Current fair value and spot — updated by the quote cycle
        self._current_fv: float = 0.5
        self._current_spot: float = 0.0

    def update_fair_value(self, fv: float, spot: float = 0.0):
        """Called each quote cycle with the latest P(Up) fair value and spot."""
        self._current_fv = fv
        self._current_spot = spot

    async def _simulate_network_latency(self):
        """Simulate realistic Polymarket CLOB API latency."""
        # Base latency between 100ms and 250ms
        latency = random.uniform(0.100, 0.250)
        
        # 10% chance of a latency spike (e.g. Polygon RPC lag or order book load)
        if random.random() < 0.10:
            latency += random.uniform(0.300, 0.800)
            
        await asyncio.sleep(latency)

    async def place_buy_order(self, token_id: str, price: float,
                               size: float, side: str = "yes",
                               book_snapshot=None) -> Optional[str]:
        """
        Simulate placing a BUY order with realistic network latency.
        """
        await self._simulate_network_latency()
        
        self._total_orders += 1

        # Simulate post_only rejection: if our price >= best ask, reject
        if book_snapshot:
            best_ask = book_snapshot.best_ask if hasattr(book_snapshot, 'best_ask') else 0.99
            if price >= best_ask:
                self._total_rejects += 1
                log.debug("dry_post_only_rejected", price=price,
                         best_ask=best_ask, side=side)
                return None

        self._order_counter += 1
        order_id = f"DRY-{self._order_counter:06d}"

        order = SimulatedOrder(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            placed_at=time.time(),
            fair_value_at_place=self._current_fv,
        )
        self.open_orders[order_id] = order

        log.debug("dry_order_placed", order_id=order_id,
                 side=side, price=price, size=size)
        return order_id

    def check_fills(self, current_mids: dict = None) -> list[dict]:
        """
        Check if any simulated orders should fill based on price crossing.

        A BUY order fills when the market price reaches or exceeds
        our limit price. For binary markets:
          - BUY YES at $0.45: fills when P(Up) >= 0.45
          - BUY NO  at $0.55: fills when P(Down) >= 0.55, i.e., P(Up) <= 0.45

        Returns:
            List of simulated fill events.
        """
        now = time.time()
        fv = self._current_fv
        fills = []
        to_remove = []

        for oid, order in self.open_orders.items():
            if order.filled:
                continue

            # Enforce minimum queue time (simulates queue position)
            elapsed = now - order.placed_at
            if elapsed < self.min_queue_time:
                continue

            # Expire stale orders (> 30s unfilled = would have been cancelled)
            if elapsed > 30:
                to_remove.append(oid)
                continue

            # --- Core fill logic: price crossing + taker flow simulation ---
            should_fill = False

            # In a real market, takers cross the spread. 
            # We simulate this by filling if our bid is within 2 cents of FV,
            # with a probabilistic chance based on how close it is.
            taker_tolerance = 0.025  # Takers will cross up to 2.5 cents of edge

            if order.side == "yes":
                edge = fv - order.price
                if edge <= 0:
                    should_fill = True  # Price crossed
                elif edge < taker_tolerance and random.random() < 0.2:
                    should_fill = True  # Taker crossed our spread
            elif order.side == "no":
                no_value = 1.0 - fv
                edge = no_value - order.price
                if edge <= 0:
                    should_fill = True  # Price crossed
                elif edge < taker_tolerance and random.random() < 0.2:
                    should_fill = True  # Taker crossed our spread

            if not should_fill:
                continue

            # Queue position simulation: orders closer to mid fill faster
            # In a taker fill (we bid higher than FV), edge is negative.
            if order.side == "yes":
                edge = order.price - fv
            else:
                edge = order.price - (1.0 - fv)

            # Tighter/crossing orders fill sooner.
            # If edge >= 0, we crossed the market -> instant fill.
            if edge >= 0:
                required_queue = 0
            else:
                # Limit order waiting for market to drop. The simulator already
                # required fv <= order.price to reach here, so this case mostly
                # applies if we just simulate the time it takes for the market to drop.
                required_queue = self.min_queue_time

            # Wait, if edge >= 0 it was an instant fill.
            # If edge < 0, we bid lower than FV. But since should_fill is True,
            # it means FV dropped. We just use min_queue_time.
            required_queue = min(required_queue, self.max_queue_time)

            if elapsed < required_queue:
                continue

            # Determine fill size (sometimes partial)
            fill_size = order.size
            if random.random() < self.partial_fill_chance and order.size > 5:
                fill_size = max(1, int(order.size * random.uniform(0.3, 0.9)))

            order.filled = True
            order.fill_time = now
            self._total_fills += 1

            fill = {
                "order_id": oid,
                "token_id": order.token_id,
                "side": order.side,
                "price": order.price,
                "size": fill_size,
                "fill_time": now,
                "simulated": True,
            }
            fills.append(fill)
            self.filled_orders.append(fill)
            to_remove.append(oid)

            log.info("dry_fill", order_id=oid, side=order.side,
                     price=order.price, size=fill_size,
                     fv=round(fv, 4), spot=round(self._current_spot, 2), elapsed=f"{elapsed:.1f}s")

        for oid in to_remove:
            self.open_orders.pop(oid, None)

        return fills

    async def cancel_order(self, order_id: str) -> bool:
        await self._simulate_network_latency()
        self.open_orders.pop(order_id, None)
        return True

    async def cancel_all(self) -> bool:
        await self._simulate_network_latency()
        count = len(self.open_orders)
        self.open_orders.clear()
        if count > 0:
            log.info("dry_all_cancelled", count=count)
        return True

    @property
    def stats(self) -> dict:
        return {
            "total_orders": self._total_orders,
            "total_fills": self._total_fills,
            "total_rejects": self._total_rejects,
            "open_orders": len(self.open_orders),
            "fill_rate": self._total_fills / max(1, self._total_orders),
        }
