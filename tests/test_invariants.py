from types import SimpleNamespace

import pytest

from src.config import AssetConfig, BotConfig
import time

from src.execution.dry_run import DryRunExecutor, SimulatedOrder
from src.execution.clob_client import ClobClientWrapper
from src.execution.order_manager import OrderManager
from src.orchestration.market_cycler import (
    MarketCycler,
    apply_dust_price_guardrails,
    compute_inventory_repair_sizes,
    should_use_close_only_repair,
)
from src.execution.state_manager import StateManager
from src.risk.toxicity import FillEdgeTracker, ToxicityMonitor
from src.strategy.inventory import InventoryManager, InventoryState
from src.strategy.quote_engine import QuoteEngine


class DummyExecutor:
    def __init__(self):
        self.calls = []

    async def place_buy_order(self, token_id, price, size, side="yes", book_snapshot=None):
        self.calls.append((token_id, price, size, side))
        return "OID-1"

    async def cancel_order(self, order_id):
        return True

    async def cancel_all(self):
        return True


class DummyBatchExecutor(DummyExecutor):
    def __init__(self):
        super().__init__()
        self.cancel_batches = []
        self.place_batches = []

    async def place_buy_orders(self, orders):
        self.place_batches.append(orders)
        return {order["side"]: f"OID-{order['side']}" for order in orders}

    async def cancel_orders(self, order_ids):
        self.cancel_batches.append(order_ids)
        return True


def test_config_validation_rejects_invalid_spreads():
    cfg = BotConfig(
        assets={
            "BTC": AssetConfig(
                enabled=True,
                symbol="BTCUSDT",
                min_spread=0.05,
                max_spread=0.03,
                soft_limit=10,
                hard_limit=20,
                emergency=30,
            )
        }
    )

    with pytest.raises(ValueError, match="min_spread"):
        cfg.validate()


def test_order_manager_places_buy_orders_through_executor():
    executor = DummyExecutor()
    om = OrderManager(executor)
    quotes = SimpleNamespace(
        yes_buy_price=0.45,
        no_buy_price=0.44,
        yes_buy_size=10,
        no_buy_size=8,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
        )
    )

    assert updated is True
    assert executor.calls == [
        ("YES1", 0.45, 10, "yes"),
        ("NO1", 0.44, 8, "no"),
    ]


def test_order_manager_batches_cancel_and_replace_when_available():
    executor = DummyBatchExecutor()
    om = OrderManager(executor, reprice_threshold=0.01)
    active = om.get_active("MARKET1")
    active.yes_order_id = "OLD-YES"
    active.no_order_id = "OLD-NO"
    active.yes_price = 0.40
    active.no_price = 0.40
    active.yes_size = 5
    active.no_size = 5

    quotes = SimpleNamespace(
        yes_buy_price=0.45,
        no_buy_price=0.44,
        yes_buy_size=10,
        no_buy_size=8,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
        )
    )

    assert updated is True
    assert executor.cancel_batches == [["OLD-YES", "OLD-NO"]]
    assert len(executor.place_batches) == 1
    assert [o["side"] for o in executor.place_batches[0]] == ["yes", "no"]
    active = om.get_active("MARKET1")
    assert active.yes_order_id == "OID-yes"
    assert active.no_order_id == "OID-no"


def test_order_manager_throttles_non_urgent_bid_improvements():
    executor = DummyBatchExecutor()
    om = OrderManager(executor, reprice_threshold=0.01, min_update_interval=2.0)
    active = om.get_active("MARKET1")
    active.yes_order_id = "OLD-YES"
    active.yes_price = 0.40
    active.yes_size = 5
    active.last_update = time.time()

    quotes = SimpleNamespace(
        yes_buy_price=0.45,  # improving/chasing bid: non-urgent
        no_buy_price=None,
        yes_buy_size=5,
        no_buy_size=0,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
        )
    )

    assert updated is False
    assert executor.cancel_batches == []
    assert executor.place_batches == []
    active = om.get_active("MARKET1")
    assert active.yes_order_id == "OLD-YES"
    assert active.yes_price == 0.40


def test_order_manager_allows_urgent_risk_reductions_during_throttle():
    executor = DummyBatchExecutor()
    om = OrderManager(executor, reprice_threshold=0.01, min_update_interval=2.0)
    active = om.get_active("MARKET1")
    active.yes_order_id = "OLD-YES"
    active.yes_price = 0.50
    active.yes_size = 5
    active.last_update = time.time()

    quotes = SimpleNamespace(
        yes_buy_price=0.45,  # lowering bid: urgent adverse-selection reduction
        no_buy_price=None,
        yes_buy_size=5,
        no_buy_size=0,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
        )
    )

    assert updated is True
    assert executor.cancel_batches == [["OLD-YES"]]
    assert len(executor.place_batches) == 1
    assert executor.place_batches[0][0]["price"] == 0.45
    active = om.get_active("MARKET1")
    assert active.yes_order_id == "OID-yes"
    assert active.yes_price == 0.45


def test_repair_quote_stays_resting_on_small_fv_wiggles():
    executor = DummyBatchExecutor()
    om = OrderManager(executor, reprice_threshold=0.01, min_update_interval=2.0)
    active = om.get_active("MARKET1")
    active.no_order_id = "OLD-NO"
    active.no_price = 0.50
    active.no_size = 6
    active.last_update = time.time() - 30

    quotes = SimpleNamespace(
        yes_buy_price=None,
        no_buy_price=0.47,  # normal mode would lower/cancel; repair should rest
        yes_buy_size=0,
        no_buy_size=6,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
            repair_mode="repair_down",
        )
    )

    assert updated is False
    assert executor.cancel_batches == []
    assert executor.place_batches == []
    assert active.no_order_id == "OLD-NO"
    assert active.no_price == 0.50


def test_repair_quote_reprices_when_dangerously_stale():
    executor = DummyBatchExecutor()
    om = OrderManager(executor, reprice_threshold=0.01, min_update_interval=2.0)
    active = om.get_active("MARKET1")
    active.no_order_id = "OLD-NO"
    active.no_price = 0.58
    active.no_size = 6
    active.last_update = time.time() - 30

    quotes = SimpleNamespace(
        yes_buy_price=None,
        no_buy_price=0.50,  # >5c lower, protect against stale overpaying
        yes_buy_size=0,
        no_buy_size=6,
    )

    import asyncio

    updated = asyncio.run(
        om.update_quotes(
            market_id="MARKET1",
            token_id_yes="YES1",
            token_id_no="NO1",
            quotes=quotes,
            repair_mode="repair_down",
        )
    )

    assert updated is True
    assert executor.cancel_batches == [["OLD-NO"]]
    assert executor.place_batches[0][0]["price"] == 0.50


def test_dust_normalization_quotes_down_tail_to_ten_each():
    # Current inventory: DOWN=3, UP=0 -> imbalance=-3.
    # Quote DOWN 7 and UP 10 so, if both fill, totals become 10/10.
    up_size, down_size, mode = compute_inventory_repair_sizes(
        imbalance=-3,
        min_order_size=5,
        max_order_size=5,
    )

    assert mode == "dust_down"
    assert up_size == 10
    assert down_size == 7


def test_dust_normalization_quotes_up_tail_to_ten_each():
    # Current inventory: UP=3, DOWN=0 -> imbalance=+3.
    up_size, down_size, mode = compute_inventory_repair_sizes(
        imbalance=3,
        min_order_size=5,
        max_order_size=5,
    )

    assert mode == "dust_up"
    assert up_size == 7
    assert down_size == 10


def test_minimum_sized_tail_does_not_force_close_only_repair_mid_window():
    assert should_use_close_only_repair(
        imbalance=-5,
        min_order_size=5,
        hard_limit=10,
        balance_only=False,
        close_only_phase=False,
        is_halted=False,
    ) is False



def test_hard_limit_tail_forces_close_only_repair_mid_window():
    assert should_use_close_only_repair(
        imbalance=-10,
        min_order_size=5,
        hard_limit=10,
        balance_only=False,
        close_only_phase=False,
        is_halted=False,
    ) is True

    up_size, down_size, mode = compute_inventory_repair_sizes(
        imbalance=-10,
        min_order_size=5,
        max_order_size=5,
    )
    assert mode == "repair_up"
    assert up_size == 5
    assert down_size == 0


def test_dust_price_guardrails_favor_repair_side_and_keep_edge():
    quotes = SimpleNamespace(
        yes_buy_price=0.49,
        no_buy_price=0.49,
        combined_cost=0.98,
        edge_per_pair=0.02,
    )

    guarded = apply_dust_price_guardrails(quotes, "dust_down")

    # Too many DOWN means UP is repair side: improve UP bid, shade DOWN bid.
    assert guarded.yes_buy_price == 0.50
    assert guarded.no_buy_price == 0.48
    assert guarded.combined_cost < 1.0


def test_quote_invariant_combined_cost_below_one():
    qe = QuoteEngine()
    quotes = qe.generate_quotes(
        fair_value=0.5,
        t_normalized=0.5,
        sigma=0.8,
        share_imbalance=0.0,
        max_imbalance=1000.0,
        yes_size=10,
        no_size=10,
    )
    assert quotes.yes_buy_price is not None
    assert quotes.no_buy_price is not None
    assert quotes.yes_buy_price + quotes.no_buy_price < 1.0


def test_emergency_inventory_behavior():
    inv = InventoryManager(emergency=100.0)
    inv.record_fill("MARKET1", "yes", 105.0, 0.5)

    state = inv.get_state("MARKET1")
    assert state == InventoryState.EMERGENCY

    up_size, down_size = inv.compute_size_adjustment("MARKET1", 0.5, base_size=10)
    assert up_size == 0
    assert down_size == 10


def test_toxicity_halt_behavior():
    tox = ToxicityMonitor(halt_cooldown=60)
    edge = FillEdgeTracker(window=10)

    for _ in range(8):
        edge.record_fill("yes", 0.50, 0.48)

    halted = tox.check_kill_switch(edge)
    assert halted is True


def test_settlement_consistency():
    inv = InventoryManager()
    inv.record_fill("MARKET1", "yes", 100.0, 0.5)
    inv.record_fill("MARKET1", "no", 100.0, 0.4)

    pnl = inv.settle_market("MARKET1", True)
    assert pnl == 100.0 - 50.0 - 40.0
    assert "MARKET1" not in inv.positions


def test_clear_market_removes_position_without_settlement_math():
    inv = InventoryManager()
    inv.record_fill("MARKET2", "yes", 5.0, 0.4)
    inv.clear_market("MARKET2")
    assert "MARKET2" not in inv.positions


def test_markets_settled_counts_unique_markets():
    from src.monitoring.pnl_tracker import PnLTracker

    pnl = PnLTracker()
    pnl.record_settlement(1.0, "MARKET1")
    pnl.record_settlement(0.5, "MARKET1")
    pnl.record_settlement(2.0, "MARKET2")

    assert pnl.markets_settled == 2


def test_toxicity_monitor_respects_conservative_thresholds():
    tox = ToxicityMonitor(
        halt_cooldown=60,
        edge_adverse_rate=0.95,
        edge_mean_threshold=0.02,
        min_fills_for_halt=8,
        one_sided_fill_limit=8,
        immediate_drift_threshold=0.02,
    )
    edge = FillEdgeTracker(window=10)

    for _ in range(7):
        edge.record_fill("yes", 0.50, 0.48)

    assert tox.check_kill_switch(edge) is False



def test_dry_run_partial_fill_keeps_order_live():
    executor = DryRunExecutor(min_queue_time=0, max_queue_time=0, partial_fill_chance=1.0)
    executor.update_fair_value(0.4, 100.0)
    executor.open_orders["DRY-1"] = SimulatedOrder(
        order_id="DRY-1",
        token_id="YES1",
        side="yes",
        price=0.5,
        size=10,
        placed_at=time.time() - 5,
    )

    first_fills = executor.check_fills()
    assert len(first_fills) == 1
    assert first_fills[0]["size"] < 10
    assert "DRY-1" in executor.open_orders
    assert executor.open_orders["DRY-1"].filled is False

    remaining = executor.open_orders["DRY-1"].size
    executor.partial_fill_chance = 0.0
    second_fills = executor.check_fills()
    assert len(second_fills) == 1
    assert second_fills[0]["size"] == remaining
    assert "DRY-1" not in executor.open_orders


def test_dry_run_rejects_fantasy_book_fill_when_side_is_valuable():
    executor = DryRunExecutor(min_queue_time=0, max_queue_time=0, partial_fill_chance=0.0)
    # YES is almost certain, so a YES bid at 3c should not magically fill in
    # dry-run. That was inflating paired P&L by creating impossible cheap legs.
    executor.update_fair_value(0.99, 100.0)
    executor.open_orders["DRY-YES"] = SimulatedOrder(
        order_id="DRY-YES",
        token_id="YES1",
        side="yes",
        price=0.03,
        size=5,
        placed_at=time.time() - 5,
    )
    book = SimpleNamespace(best_bid=0.03, best_ask=0.03)

    fills = executor.check_fills(yes_book_snapshot=book)

    assert fills == []
    assert "DRY-YES" in executor.open_orders


def test_dry_run_allows_adverse_book_fill_when_side_value_falls():
    executor = DryRunExecutor(min_queue_time=0, max_queue_time=0, partial_fill_chance=0.0)
    # YES fell below our bid; this is a plausible adverse maker fill.
    executor.update_fair_value(0.40, 100.0)
    executor.open_orders["DRY-YES"] = SimulatedOrder(
        order_id="DRY-YES",
        token_id="YES1",
        side="yes",
        price=0.42,
        size=5,
        placed_at=time.time() - 5,
    )
    book = SimpleNamespace(best_bid=0.42, best_ask=0.42)

    fills = executor.check_fills(yes_book_snapshot=book)

    assert len(fills) == 1
    assert fills[0]["side"] == "yes"
    assert fills[0]["price"] == 0.42


def test_dry_run_rejects_grossly_toxic_stale_book_fill():
    executor = DryRunExecutor(
        min_queue_time=0,
        max_queue_time=0,
        partial_fill_chance=0.0,
        max_fair_value_dislocation=0.03,
    )
    # NO is worth only 4c, so a NO bid at 18c is a stale-book/toxic
    # execution artifact. Counting it as normal dry-run maker flow made pair
    # costs look far cheaper than live.
    executor.update_fair_value(0.96, 100.0)
    executor.open_orders["DRY-NO"] = SimulatedOrder(
        order_id="DRY-NO",
        token_id="NO1",
        side="no",
        price=0.18,
        size=5,
        placed_at=time.time() - 5,
    )
    book = SimpleNamespace(best_bid=0.18, best_ask=0.18)

    fills = executor.check_fills(no_book_snapshot=book)

    assert fills == []
    assert "DRY-NO" in executor.open_orders


def test_state_manager_inventory_updates_merge_without_erasing_other_assets(tmp_path):
    state = StateManager(str(tmp_path / "state.json"))

    state.update_inventory({"BTC-MARKET": {"market_id": "BTC-MARKET", "asset": "BTC"}})
    state.update_inventory({"ETH-MARKET": {"market_id": "ETH-MARKET", "asset": "ETH"}})

    assert set(state.state["inventory"]) == {"BTC-MARKET", "ETH-MARKET"}

    state.remove_inventory_market("BTC-MARKET")
    assert set(state.state["inventory"]) == {"ETH-MARKET"}


def test_live_settlement_keeps_inventory_when_merge_fails():
    import asyncio

    class DummyOrderManager:
        async def cancel_market_quotes(self, market_id):
            return True

    class FailingCtf:
        async def merge_positions(self, condition_id, amount):
            return None

        async def is_market_resolved(self, condition_id):
            return False

    class DummyPnl:
        net_pnl = 0.0

        def record_settlement(self, *args, **kwargs):
            raise AssertionError("settlement should not be recorded on failed merge")

        def record_capital_recovery(self, *args, **kwargs):
            raise AssertionError("capital should not recover on failed merge")

    inv = InventoryManager()
    inv.record_fill("MARKET1", "yes", 5, 0.40, "BTC")
    inv.record_fill("MARKET1", "no", 5, 0.50, "BTC")

    cycler = MarketCycler.__new__(MarketCycler)
    cycler.current_market = SimpleNamespace(
        market_id="MARKET1",
        condition_id="COND1",
        slug="slug",
    )
    cycler.asset = "BTC"
    cycler.order_mgr = DummyOrderManager()
    cycler.inventory = inv
    cycler.pnl = DummyPnl()
    cycler.ctf = FailingCtf()
    cycler.gasless_merger = None
    cycler.quote_engine = SimpleNamespace(reset_params=lambda: None)
    cycler.portfolio_pnl_getter = None
    cycler.risk_engine = SimpleNamespace(reset_for_new_market=lambda pnl: None)

    asyncio.run(cycler._settle_market())

    assert "MARKET1" in inv.positions


def test_live_fill_side_uses_token_id_when_order_context_missing():
    client = ClobClientWrapper("host", "pk", 137, "key", "secret", "pass")
    raw = [{
        "id": "T1",
        "orderID": "UNKNOWN",
        "asset_id": "NO-TOKEN",
        "price": "0.42",
        "size": "7",
    }]

    fills = client.process_fills(
        raw,
        inventory_mgr=None,
        market_id="MARKET1",
        token_id_yes="YES-TOKEN",
        token_id_no="NO-TOKEN",
    )

    assert fills == [{
        "order_id": "UNKNOWN",
        "token_id": "NO-TOKEN",
        "side": "no",
        "price": 0.42,
        "size": 7.0,
        "fill_time": fills[0]["fill_time"],
        "simulated": False,
    }]


def test_fill_recording_feeds_delayed_toxicity_monitor():
    class DummyPnl:
        def record_fill(self, **kwargs):
            self.kwargs = kwargs

    cycler = MarketCycler.__new__(MarketCycler)
    cycler.asset = "BTC"
    cycler.inventory = InventoryManager()
    cycler.pnl = DummyPnl()
    cycler.edge_tracker = FillEdgeTracker(window=10)
    cycler.toxicity_monitor = ToxicityMonitor()

    cycler._record_fill_effects(
        "MARKET1",
        {"side": "yes", "price": 0.45, "size": 3},
        fv=0.44,
    )

    assert len(cycler.edge_tracker.fills) == 1
    assert len(cycler.toxicity_monitor.fill_history) == 1
    assert cycler.toxicity_monitor.fill_history[0]["mid_at_fill"] == 0.44
