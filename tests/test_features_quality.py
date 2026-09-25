"""Data Quality Policy, gap classification, no silent fill, insufficient history,
closed-candle protection."""
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from app.domain.enums import FeatureStatus, GapSeverity, QualityStatus
from app.domain.market import Symbol, Timeframe
from app.research.engine import FeatureEngine
from app.research.quality import DataQualityPolicy
from tests.feature_data import BTC, T0, bar, end_of, poisoned, walk

M15, H1 = Timeframe.M15, Timeframe.H1
P = DataQualityPolicy()
E = FeatureEngine()


def feat(candles, name, as_of=None, tf=M15):
    return E.snapshot(candles, tf, as_of or end_of(candles), [name], symbol=BTC).features[name]


class DataQualityPolicyTests(unittest.TestCase):
    def test_valid_series(self):
        cs = walk(50)
        s = P.prepare(cs, M15, end_of(cs))
        self.assertEqual((len(s), s.stats.gaps, s.stale, s.unusable), (50, 0, False, None))
        a = P.assess(s, 20)
        self.assertEqual((a.status, a.quality, a.severity), (FeatureStatus.VALUE, QualityStatus.VALID,
                                                             GapSeverity.NO_GAP))

    def test_missing_history(self):
        s = P.prepare([], M15, T0 + timedelta(days=1), BTC)
        a = P.assess(s, 2)
        self.assertEqual((a.status, a.quality), (FeatureStatus.INSUFFICIENT_HISTORY, QualityStatus.MISSING_HISTORY))

    def test_stale_when_latest_bar_missing(self):
        cs = walk(50)
        s = P.prepare(cs, M15, end_of(cs) + M15.delta)   # one more bar should have closed
        self.assertTrue(s.stale)
        a = P.assess(s, 2)
        self.assertEqual((a.status, a.quality, a.severity),
                         (FeatureStatus.DATA_QUALITY_FAILURE, QualityStatus.STALE, GapSeverity.UNUSABLE))

    def test_decision_time_inside_bar_is_not_stale(self):
        cs = walk(50)
        self.assertFalse(P.prepare(cs, M15, end_of(cs) + timedelta(minutes=14)).stale)

    def test_duplicate_makes_series_unusable(self):
        cs = walk(30)
        s = P.prepare(cs + [cs[10]], M15, end_of(cs))
        self.assertEqual(s.unusable, QualityStatus.DUPLICATE)
        self.assertEqual(P.assess(s, 2).status, FeatureStatus.DATA_QUALITY_FAILURE)

    def test_foreign_symbol_or_timeframe_is_invalid(self):
        cs = walk(30)
        eth = walk(1, symbol=Symbol("ETHUSDT"))
        for extra in (eth, walk(1, H1)):
            with self.subTest(extra=extra[0].symbol.name + extra[0].timeframe.value):
                s = P.prepare(cs + extra, M15, end_of(cs), BTC)
                self.assertEqual(s.unusable, QualityStatus.INVALID)
                self.assertEqual(s.stats.invalid_observations, 1)

    def test_open_and_future_bars_counted_not_used(self):
        cs = walk(30)
        forming = replace(cs[-1], open_time=cs[-1].open_time + M15.delta, is_closed=False)
        s = P.prepare(cs + [forming], M15, end_of(cs) + timedelta(minutes=5))
        self.assertEqual((len(s), s.stats.open_candle_exclusions), (30, 1))
        s2 = P.prepare(cs, M15, cs[20].close_time)
        self.assertEqual((len(s2), s2.stats.future_exclusions), (21, 9))


class GapClassificationTests(unittest.TestCase):
    def test_major_gap_inside_window(self):  # Test 3
        cs = walk(900, missing={850})
        f = feat(cs, "ema_200")
        self.assertEqual((f.status, f.quality, f.gap_severity),
                         (FeatureStatus.DATA_QUALITY_FAILURE, QualityStatus.GAP, GapSeverity.MAJOR_GAP))
        self.assertIsNone(f.value)
        self.assertIn("recovers after", f.reason)

    def test_severity_is_feature_specific(self):
        cs = walk(900, missing={850})              # gap 49 bars before the end
        self.assertEqual(feat(cs, "sma_20").gap_severity, GapSeverity.MINOR_GAP)   # window 20: unaffected
        self.assertEqual(feat(cs, "sma_20").status, FeatureStatus.VALUE)
        self.assertEqual(feat(cs, "sma_50").status, FeatureStatus.DATA_QUALITY_FAILURE)  # window 50: hit
        self.assertEqual(feat(cs, "ema_200").gap_severity, GapSeverity.MAJOR_GAP)

    def test_old_gap_is_minor_the_phase1_698_of_700_case(self):
        cs = walk(1700, missing={100})              # 1 missing bar far in the past
        f = feat(cs, "ema_200")
        self.assertEqual((f.status, f.gap_severity), (FeatureStatus.VALUE, GapSeverity.MINOR_GAP))
        self.assertEqual(f.bars_used, 800)

    def test_recovery_after_enough_new_bars(self):
        cs = walk(1900, missing={1000})
        after = [c for c in cs if c.open_time > T0 + M15.delta * 1000]
        needed_close = after[798].close_time      # 799 contiguous bars after the gap
        self.assertEqual(feat(cs, "ema_200", needed_close).status, FeatureStatus.DATA_QUALITY_FAILURE)
        self.assertEqual(feat(cs, "ema_200", needed_close + M15.delta).status, FeatureStatus.VALUE)

    def test_short_history_with_gap_is_insufficient_not_gap(self):
        cs = walk(300, missing={100})
        f = feat(cs, "ema_200")
        self.assertEqual(f.status, FeatureStatus.INSUFFICIENT_HISTORY)

    def test_gap_tolerance_disabled_even_with_rationale(self):
        """Tolerating a gap would hand the formula non-adjacent bars as if adjacent."""
        from app.research.features.catalog import f_sma
        from app.research.features.spec import FeatureSpec
        from app.research.quality import DataQualityPolicy
        with self.assertRaisesRegex(ValueError, "glue"):
            FeatureSpec("x", 1, "trend", "quote", "d", f_sma, 5, 5, params={"n": 5}, max_gap_bars=1,
                        gap_rationale="missing at random")
        s = DataQualityPolicy().prepare(walk(30, missing={10}), M15, walk(30)[-1].close_time)
        with self.assertRaisesRegex(ValueError, "glue"):
            DataQualityPolicy().assess(s, 5, max_gap_bars=2)


class NoSilentFillTests(unittest.TestCase):  # Test 4
    def test_gap_is_not_filled(self):
        cs = walk(100, missing={60, 61})
        s = P.prepare(cs, M15, end_of(cs))
        self.assertEqual(len(s), 98)                              # no synthetic bars
        self.assertEqual(s.times, [c.open_time for c in cs])      # only real timestamps
        self.assertEqual(s.stats.missing_observations, 2)
        self.assertEqual(s.c, [float(c.close) for c in cs])       # no copied closes

    def test_return_across_gap_not_computed(self):
        cs = walk(100, missing={60})
        first_after = next(c for c in cs if c.open_time == T0 + M15.delta * 61)
        f = feat(cs, "ret_cc", first_after.close_time)
        self.assertEqual(f.status, FeatureStatus.DATA_QUALITY_FAILURE)   # would be a 2-bar return
        g = feat(cs, "ret_cc", first_after.close_time + M15.delta)
        self.assertEqual(g.status, FeatureStatus.VALUE)

    def test_no_zero_or_previous_value_on_failure(self):
        cs = walk(100, missing={95})
        snap = E.snapshot(cs, M15, end_of(cs), symbol=BTC)
        for key, f in snap.features.items():
            if f.status is not FeatureStatus.VALUE:
                self.assertIsNone(f.value, key)
                self.assertTrue(f.reason, key)


class InsufficientHistoryTests(unittest.TestCase):
    def test_ema200_warmup(self):  # Test 2
        cs = walk(800)
        self.assertEqual(feat(cs[:799], "ema_200").status, FeatureStatus.INSUFFICIENT_HISTORY)
        self.assertEqual(feat(cs[:799], "ema_200").reason, "799/800 contiguous closed bars")
        self.assertEqual(feat(cs, "ema_200").status, FeatureStatus.VALUE)

    def test_ema200_not_computed_on_last_200_only(self):
        f = feat(walk(200), "ema_200")
        self.assertEqual(f.status, FeatureStatus.INSUFFICIENT_HISTORY)

    def test_atr_failure_has_status_not_fallback(self):  # Test 6
        f = feat(walk(95), "atr_14")
        self.assertEqual((f.status, f.value), (FeatureStatus.INSUFFICIENT_HISTORY, None))
        f2 = E.snapshot([], M15, T0 + timedelta(days=1), ["atr_14_pct"], symbol=BTC).features["atr_14_pct"]
        self.assertEqual((f2.status, f2.quality, f2.value),
                         (FeatureStatus.INSUFFICIENT_HISTORY, QualityStatus.MISSING_HISTORY, None))

    def test_requirements_centralised_and_derived(self):
        from app.research.features.catalog import DEFAULT_REGISTRY as R
        self.assertEqual((R.get("ema_200").warmup, R.get("ema_200").min_observations), (600, 800))
        self.assertEqual((R.get("ema_50").warmup, R.get("ema_50").min_observations), (150, 200))
        self.assertEqual((R.get("atr_14").warmup, R.get("atr_14").min_observations), (81, 96))


class ClosedCandleTests(unittest.TestCase):  # Test 10
    def test_open_candle_never_used(self):
        cs = walk(900)
        as_of = end_of(cs) + timedelta(minutes=7)          # inside the next, still forming bar
        forming = replace(poisoned(cs[-1]), open_time=end_of(cs), is_closed=False)
        clean = E.snapshot(cs, M15, as_of, symbol=BTC)
        dirty = E.snapshot(cs + [forming], M15, as_of, symbol=BTC)
        self.assertEqual(clean.features, dirty.features)

    def test_bar_marked_closed_but_not_yet_closed_is_ignored(self):
        cs = walk(900)
        as_of = end_of(cs) + timedelta(minutes=7)
        early = replace(poisoned(cs[-1]), open_time=end_of(cs), is_closed=True)   # wrong flag upstream
        self.assertEqual(E.snapshot(cs, M15, as_of, symbol=BTC).features,
                         E.snapshot(cs + [early], M15, as_of, symbol=BTC).features)

    def test_specs_cannot_opt_into_open_candles(self):
        from app.research.features.catalog import f_volume
        from app.research.features.spec import FeatureSpec
        with self.assertRaises(ValueError):
            FeatureSpec("live_volume", 1, "volume", "base", "d", f_volume, 1, 1, requires_closed=False)
