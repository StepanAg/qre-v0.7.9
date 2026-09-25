import unittest
from datetime import datetime
from decimal import Decimal as D

from app.domain.decision import EntryPlan, RiskPlan
from app.domain.enums import (EventSource, OrderStatus, OrderType, PositionSide, Side,
                              TimeInForce)
from app.domain.errors import DomainError, InvalidTransition
from app.domain.execution import Order, filled_qty
from app.domain.market import Candle, InstrumentInfo, OrderBookSnapshot, Symbol, Timeframe
from app.domain.research import Feature, FeatureSnapshot
from tests.factories import BTC, fill, ts


def order(**kw):
    base = dict(local_order_id="ord_1", client_order_id="qabc", symbol=BTC, side=Side.BUY,
                order_type=OrderType.LIMIT, qty=D("1"), price=D("100"), created_at=ts(), updated_at=ts())
    base.update(kw)
    return Order(**base)


class MarketTests(unittest.TestCase):
    def test_symbol_upper(self):
        with self.assertRaises(DomainError):
            Symbol("btcusdt")

    def test_candle_validation(self):
        Candle(BTC, Timeframe.M15, ts(), D("100"), D("110"), D("95"), D("105"), D("10"))
        with self.assertRaises(DomainError):
            Candle(BTC, Timeframe.M15, ts(), D("100"), D("99"), D("95"), D("105"), D("10"))
        with self.assertRaises(DomainError):
            Candle(BTC, Timeframe.M15, datetime(2026, 1, 1), D("1"), D("1"), D("1"), D("1"), D("1"))

    def test_float_rejected(self):
        with self.assertRaises(DomainError):
            Candle(BTC, Timeframe.M15, ts(), 100.0, D("110"), D("95"), D("105"), D("10"))

    def test_instrument_rounding(self):
        i = InstrumentInfo(BTC, D("0.1"), D("0.001"), D("0.001"), D("5"))
        self.assertEqual(i.round_qty_down(D("0.0129")), D("0.012"))
        self.assertEqual(i.round_price(D("100.04")), D("100.0"))

    def test_crossed_book(self):
        with self.assertRaises(DomainError):
            OrderBookSnapshot(BTC, ts(), ((D("101"), D("1")),), ((D("100"), D("1")),))


class ResearchTests(unittest.TestCase):
    def test_feature_insufficient_history_cannot_be_valid(self):
        from app.domain.enums import FeatureStatus
        with self.assertRaises(DomainError):
            Feature("ema_200", 1.0, Timeframe.M15, ts(), FeatureStatus.VALUE, min_observations=800, bars_used=50)
        with self.assertRaises(DomainError):   # no silent value on failure
            Feature("ema_200", 0.0, Timeframe.M15, ts(), FeatureStatus.INSUFFICIENT_HISTORY, reason="50/800")
        with self.assertRaises(DomainError):
            Feature("ema_200", float("nan"), Timeframe.M15, ts(), FeatureStatus.VALUE)
        f = Feature("ema_200", None, Timeframe.M15, ts(), FeatureStatus.INSUFFICIENT_HISTORY,
                    min_observations=800, bars_used=50, reason="50/800 bars")
        snap = FeatureSnapshot(BTC, ts(), {"ema_200": f})
        with self.assertRaises(DomainError):
            snap.get("ema_200")
        with self.assertRaises(KeyError):
            snap.get("ema200")  # typo in field name is loud, not 0
        self.assertFalse(snap.is_ready("ema_200"))


class DecisionTests(unittest.TestCase):
    def test_risk_plan_stop_side(self):
        with self.assertRaises(DomainError):
            RiskPlan("r", "s", PositionSide.LONG, D("100"), D("105"), D("1"))
        rp = RiskPlan("r", "s", PositionSide.SHORT, D("100"), D("105"), D("2"))
        self.assertEqual(rp.planned_risk_amount, D("10"))

    def test_risk_plan_frozen(self):
        rp = RiskPlan("r", "s", PositionSide.LONG, D("100"), D("90"), D("1"))
        with self.assertRaises(Exception):
            rp.initial_stop = D("95")  # type: ignore[misc]

    def test_entry_plan_zone(self):
        with self.assertRaises(DomainError):
            EntryPlan("e", "s", D("101"), D("100"), OrderType.LIMIT, TimeInForce.POST_ONLY, ts())


class OrderTests(unittest.TestCase):
    def test_lifecycle(self):
        o = order()
        o = o.transition(OrderStatus.SUBMITTING, ts(1))
        o = o.transition(OrderStatus.ACCEPTED, ts(2), exchange_order_id="ex1")
        o = o.transition(OrderStatus.PARTIALLY_FILLED, ts(3))
        o = o.transition(OrderStatus.FILLED, ts(4))
        self.assertTrue(o.status.is_terminal)
        self.assertEqual(o.exchange_order_id, "ex1")
        with self.assertRaises(InvalidTransition):
            o.transition(OrderStatus.CANCELLED, ts(5))

    def test_unknown_state_after_lost_ack(self):
        o = order().transition(OrderStatus.SUBMITTING, ts(1)).transition(OrderStatus.UNKNOWN, ts(2))
        o = o.transition(OrderStatus.FILLED, ts(3))  # resolved by reconciliation
        self.assertEqual(o.status, OrderStatus.FILLED)

    def test_limit_requires_price_and_link_id_limit(self):
        with self.assertRaises(DomainError):
            order(price=None)
        with self.assertRaises(DomainError):
            order(client_order_id="x" * 37)

    def test_filled_qty_from_fills_dedups(self):
        o = order()
        fs = [fill("e1", Side.BUY, "0.4", "100"), fill("e1", Side.BUY, "0.4", "100"),
              fill("e2", Side.BUY, "0.6", "100")]
        self.assertEqual(filled_qty(o, fs), D("1.0"))
        with self.assertRaises(DomainError):
            filled_qty(o, fs + [fill("e3", Side.BUY, "0.1", "100")])

    def test_signal_order_trade_are_distinct_types(self):
        from app.domain.accounting import Trade
        from app.domain.decision import Signal
        from app.domain.execution import Fill, Position
        types = {Signal, Order, Fill, Position, Trade}
        self.assertEqual(len(types), 5)
        for a in types:
            for b in types - {a}:
                self.assertFalse(issubclass(a, b))

    def test_source_tag(self):
        self.assertEqual(order().source, EventSource.LOCAL)
