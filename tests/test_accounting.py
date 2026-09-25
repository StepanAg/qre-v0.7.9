import unittest
from decimal import Decimal as D

from app.analytics.metrics import trade_metrics
from app.domain.accounting import FundingEvent
from app.domain.enums import ExitReason, FillRole, PositionSide, Side, TradeStatus
from app.domain.errors import InvariantViolation
from tests.factories import BTC, fill, trade, ts

E, X = FillRole.ENTRY, FillRole.EXIT


class TradeLifecycleTests(unittest.TestCase):
    def test_partial_fills_and_partial_closes(self):
        t = trade()
        t = t.apply_fill(fill("e1", Side.BUY, "0.4", "100", fee="0.02", sec=1), E)
        t = t.apply_fill(fill("e2", Side.BUY, "0.6", "101", fee="0.03", sec=2), E)
        self.assertEqual(t.status, TradeStatus.OPEN)
        self.assertEqual(t.avg_entry, D("100.6"))
        t = t.apply_fill(fill("x1", Side.SELL, "0.5", "110", fee="0.05", sec=3), X)
        self.assertEqual(t.open_qty, D("0.5"))
        self.assertEqual(t.status, TradeStatus.OPEN)
        t = t.apply_fill(fill("x2", Side.SELL, "0.5", "90", fee="0.05", sec=4), X)
        self.assertEqual(t.status, TradeStatus.CLOSED)
        p = t.pnl()
        # (110-100.6)*0.5 + (90-100.6)*0.5 = 4.7 - 5.3 = -0.6
        self.assertEqual(p.gross, D("-0.6"))
        self.assertEqual(p.fees, D("0.15"))
        self.assertEqual(p.net, D("-0.75"))

    def test_duplicate_fill_is_idempotent(self):
        t = trade().apply_fill(fill("e1", Side.BUY, "1", "100", sec=1), E)
        t2 = t.apply_fill(fill("e1", Side.BUY, "1", "100", sec=1), E)
        self.assertIs(t, t2)

    def test_phantom_or_double_close_rejected(self):
        t = trade().apply_fill(fill("e1", Side.BUY, "1", "100", sec=1), E)
        t = t.apply_fill(fill("x1", Side.SELL, "1", "105", sec=2), X)
        with self.assertRaises(InvariantViolation):
            t.apply_fill(fill("x2", Side.SELL, "1", "105", sec=3), X)

    def test_exit_exceeding_open_rejected(self):
        t = trade().apply_fill(fill("e1", Side.BUY, "0.3", "100", sec=1), E)
        with self.assertRaises(InvariantViolation):
            t.apply_fill(fill("x1", Side.SELL, "0.5", "105", sec=2), X)

    def test_wrong_side_rejected(self):
        with self.assertRaises(InvariantViolation):
            trade().apply_fill(fill("e1", Side.SELL, "1", "100"), E)

    def test_out_of_order_arrival_replays_by_time(self):
        # An exit arriving before its entry is rejected (ingestion must buffer
        # and retry) instead of creating a negative/phantom position.
        with self.assertRaises(InvariantViolation):
            trade().apply_fill(fill("x1", Side.SELL, "1", "110", sec=5), X)
        # Legs are replayed by exec_time, not by arrival order:
        t = trade().apply_fill(fill("e2", Side.BUY, "0.5", "102", sec=2), E)
        t = t.apply_fill(fill("e1", Side.BUY, "0.5", "98", sec=1), E)
        t = t.apply_fill(fill("x1", Side.SELL, "1", "110", sec=5), X)
        self.assertEqual(t.pnl().gross, D("10"))

    def test_short_pnl(self):
        t = trade(PositionSide.SHORT, entry="100", stop="110")
        t = t.apply_fill(fill("e1", Side.SELL, "2", "100", sec=1), E)
        t = t.apply_fill(fill("x1", Side.BUY, "2", "95", sec=2), X)
        self.assertEqual(t.pnl().gross, D("10"))
        self.assertEqual(t.realized_r(), D("0.5"))  # 10 / (10*2)

    def test_pnl_decomposition_with_funding_and_slippage(self):
        t = trade()
        t = t.apply_fill(fill("e1", Side.BUY, "1", "100.5", fee="0.1", sec=1, expected="100"), E)
        t = t.apply_fill(fill("x1", Side.SELL, "1", "109.5", fee="0.1", sec=9, expected="110"), X)
        t = t.apply_funding(FundingEvent("fnd_1", BTC, D("-0.3"), "USDT", ts(5), "bybit-tx-1"))
        t = t.apply_funding(FundingEvent("fnd_1", BTC, D("-0.3"), "USDT", ts(5), "bybit-tx-1"))  # dup
        p = t.pnl()
        self.assertEqual(p.gross, D("9.0"))
        self.assertEqual(p.slippage_cost, D("1.0"))
        self.assertEqual(p.theoretical_gross, D("10.0"))
        self.assertEqual(p.funding, D("-0.3"))
        self.assertEqual(p.net, D("8.5"))  # 9.0 - 0.2 - 0.3
        self.assertEqual(p.theoretical_gross - p.slippage_cost - p.fees + p.funding, p.net)

    def test_initial_risk_uses_frozen_stop_and_actual_fills(self):
        t = trade(entry="100", stop="90", qty="1")
        t = t.apply_fill(fill("e1", Side.BUY, "0.5", "100", sec=1), E)  # partial entry only
        t = t.apply_fill(fill("x1", Side.SELL, "0.5", "120", sec=2), X)
        self.assertEqual(t.initial_risk_amount, D("5"))   # not planned 10
        self.assertEqual(t.realized_r(), D("2"))

    def test_r_none_until_closed(self):
        t = trade().apply_fill(fill("e1", Side.BUY, "1", "100", sec=1), E)
        self.assertIsNone(t.realized_r())

    def test_fill_after_close_rejected(self):
        t = trade().apply_fill(fill("e1", Side.BUY, "1", "100", sec=1), E)
        t = t.apply_fill(fill("x1", Side.SELL, "1", "105", sec=2), X)
        with self.assertRaises(InvariantViolation):
            t.apply_fill(fill("e2", Side.BUY, "1", "100", sec=3), E)

    def test_exit_reason_single_assignment(self):
        t = trade().apply_fill(fill("e1", Side.BUY, "1", "100", sec=1), E)
        with self.assertRaises(InvariantViolation):
            t.close_with_reason(ExitReason.STOP_LOSS)
        t = t.apply_fill(fill("x1", Side.SELL, "1", "90", sec=2), X).close_with_reason(ExitReason.STOP_LOSS)
        with self.assertRaises(InvariantViolation):
            t.close_with_reason(ExitReason.TAKE_PROFIT)

    def test_metrics_exclude_open_trades(self):
        closed = trade().apply_fill(fill("e1", Side.BUY, "1", "100", sec=1), E) \
                        .apply_fill(fill("x1", Side.SELL, "1", "110", sec=2), X)
        opened = trade().apply_fill(fill("e9", Side.BUY, "1", "100", sec=1), E)
        m = trade_metrics([closed, opened, trade()])
        self.assertEqual(m.trades, 1)
        self.assertEqual(m.net_pnl, D("10"))
        self.assertEqual(m.avg_r, D("1"))
