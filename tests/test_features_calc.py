"""Feature formulas, registry, CVD naming, NaN/Inf, versioning, determinism."""
import json
import math
import random
import statistics
import unittest
from pathlib import Path

from app.domain.enums import FeatureStatus, QualityStatus
from app.domain.market import Timeframe
from app.research.engine import FeatureEngine, feature_set_hash
from app.research.features import catalog as C
from app.research.features.catalog import DEFAULT_REGISTRY as R
from app.research.features.methodology import (ema_alpha, ema_last, recursive_warmup, sample_std,
                                               true_ranges, wilder_last)
from app.research.features.spec import FeatureRegistry, FeatureSpec, Window
from tests.feature_data import BTC, bar, end_of, walk

M15 = Timeframe.M15
E = FeatureEngine()


def win(cs):
    f = lambda a: [float(getattr(c, a)) for c in cs]  # noqa: E731
    return Window(cs[0].timeframe, f("open"), f("high"), f("low"), f("close"), f("volume"), f("turnover"))


def value(cs, name, as_of=None):
    f = E.snapshot(cs, M15, as_of or end_of(cs), [name], symbol=BTC).features[name]
    assert f.status is FeatureStatus.VALUE, (name, f.status, f.reason)
    return f.value


def ref_ema(closes, n):
    """Independent reference: textbook recursion written differently."""
    a = 2 / (n + 1)
    e = sum(closes[:n]) / n
    for i in range(n, len(closes)):
        e = e + a * (closes[i] - e)
    return e


class FeatureRegistryTests(unittest.TestCase):
    def test_every_feature_has_metadata(self):
        for s in R.latest():
            m = s.metadata()
            for k in ("name", "version", "family", "unit", "lookback", "warmup", "min_observations",
                      "requires_closed", "max_gap_bars", "formula_hash"):
                self.assertIn(k, m, s.name)
            self.assertIn(s.family, {"returns", "trend", "volatility", "volume", "structure"})
            self.assertTrue(s.unit)

    def test_required_features_present(self):
        for n in ("ema_50", "ema_200", "sma_20", "atr_14", "atr_14_pct", "ret_std_20", "realized_vol_20",
                  "vol_20_annualized", "vol_ratio_20_100", "ret_cc", "log_ret", "ret_oc", "true_range",
                  "hl_range_pct", "ret_20", "volume", "turnover", "rel_volume_20", "turnover_sma_20",
                  "vt_anomaly_count_20", "rolling_high_20", "dist_high_20_pct", "amihud_20"):
            self.assertIn(n, R.names())

    def test_get_versions_and_duplicates(self):
        reg = FeatureRegistry(R.all_versions())
        v2 = FeatureSpec("sma_20", 2, "trend", "quote", "v2", C.f_sma, 20, 20, params={"n": 20})
        reg.register(v2)
        self.assertEqual(reg.get("sma_20").version, 2)
        self.assertEqual(reg.get("sma_20@1").version, 1)
        with self.assertRaises(ValueError):
            reg.register(v2)
        with self.assertRaises(KeyError):
            reg.get("sma_20@9")

    def test_units_declared_correctly(self):
        self.assertEqual((R.get("volume").unit, R.get("turnover").unit), ("base", "quote"))
        self.assertEqual(R.get("volume_sum_20").unit, "base")
        self.assertEqual(R.get("turnover_sma_20").unit, "quote")


class CVDNamingTests(unittest.TestCase):  # Test 12
    def test_true_cvd_not_implemented(self):
        f = E.snapshot(walk(50), M15, end_of(walk(50)), ["cvd"], symbol=BTC).features["cvd"]
        self.assertEqual((f.status, f.value), (FeatureStatus.NOT_AVAILABLE, None))
        self.assertIn("trade-level", f.reason)

    def test_proxy_is_labelled_as_proxy(self):
        p = R.get("clv_volume_pressure_proxy_20")
        self.assertTrue(p.is_proxy)
        self.assertEqual(p.proxy_for, "cvd")
        for s in R.latest():
            if s.implemented and "cvd" in (s.name + s.description).lower():
                self.fail(f"{s.name} mentions CVD but is implemented")
            if s.is_proxy:
                self.assertIn("proxy", s.name)

    def test_proxy_bounded(self):
        v = value(walk(100), "clv_volume_pressure_proxy_20")
        self.assertTrue(-1.0 <= v <= 1.0)


class EMATests(unittest.TestCase):
    def test_hand_calculation(self):
        self.assertEqual(ema_last([1, 2, 3, 4, 5], 3), 4.0)   # seed 2, a=0.5 -> 3 -> 4

    def test_matches_reference(self):
        cs = walk(1000)
        closes = [float(c.close) for c in cs]
        for n, name in ((50, "ema_50"), (200, "ema_200")):
            m = n + recursive_warmup(ema_alpha(n))
            self.assertAlmostEqual(value(cs, name), ref_ema(closes[-m:], n), places=8)

    def test_determinism(self):  # Test 1
        cs = walk(1000)
        a = value(cs, "ema_200")
        b = FeatureEngine().snapshot(list(cs), M15, end_of(cs), ["ema_200"], symbol=BTC).features["ema_200"].value
        self.assertEqual(a.hex(), b.hex())                       # bit-identical

    def test_independent_of_loaded_history_length(self):
        cs = walk(3000)
        self.assertEqual(value(cs[-800:], "ema_200"), value(cs, "ema_200"))

    def test_slope_and_distance_consistent_with_ema(self):
        cs = walk(1000)
        e_now, e_prev = value(cs, "ema_50"), value(cs[:-10], "ema_50")
        self.assertAlmostEqual(value(cs, "ema_50_slope_10"), e_now / e_prev - 1, places=12)
        self.assertAlmostEqual(value(cs, "dist_ema_50_pct"), float(cs[-1].close) / e_now - 1, places=12)
        self.assertAlmostEqual(value(cs, "ema_50_200_spread"), e_now / value(cs, "ema_200") - 1, places=12)


class SMATests(unittest.TestCase):
    def test_sma(self):
        cs = walk(60)
        self.assertAlmostEqual(value(cs, "sma_20"), statistics.fmean(float(c.close) for c in cs[-20:]), places=9)
        self.assertAlmostEqual(value(cs, "sma_50"), statistics.fmean(float(c.close) for c in cs[-50:]), places=9)


class ATRTests(unittest.TestCase):  # Test 5
    FIX = [bar(0, 10, 11, 9, 10), bar(1, 10, 12, 10, 11), bar(2, 11, 13, 10, 12),
           bar(3, 12, 12, 11, 11), bar(4, 11, 14, 11, 14), bar(5, 14, 15, 13, 13)]

    def test_hand_fixture_wilder_atr3(self):
        w = win(self.FIX)
        self.assertEqual(true_ranges(w.h, w.l, w.c), [2, 3, 1, 3, 2])
        self.assertAlmostEqual(C.f_atr(w, 3), 20 / 9, places=12)   # seed 2 -> 7/3 -> 20/9

    def test_atr14_matches_reference(self):
        cs = walk(300)
        w = win(cs[-96:])
        tr = [max(w.h[i] - w.l[i], abs(w.h[i] - w.c[i - 1]), abs(w.l[i] - w.c[i - 1])) for i in range(1, 96)]
        atr = sum(tr[:14]) / 14
        for x in tr[14:]:
            atr = atr + (x - atr) / 14
        self.assertAlmostEqual(value(cs, "atr_14"), atr, places=9)
        self.assertAlmostEqual(value(cs, "atr_14_pct"), atr / float(cs[-1].close), places=12)

    def test_no_hidden_fallback_in_code(self):
        src = Path(C.__file__).read_text()
        self.assertNotIn("0.015", src)
        self.assertNotIn("fallback", src.lower())


class VolatilityTests(unittest.TestCase):
    def test_std_realized_annualized(self):
        cs = walk(200)
        cl = [float(c.close) for c in cs[-21:]]
        r = [math.log(cl[i] / cl[i - 1]) for i in range(1, 21)]
        self.assertAlmostEqual(value(cs, "ret_std_20"), statistics.stdev(r), places=12)
        self.assertAlmostEqual(value(cs, "realized_vol_20"), math.sqrt(sum(x * x for x in r)), places=12)
        self.assertAlmostEqual(value(cs, "vol_20_annualized"), statistics.stdev(r) * math.sqrt(365 * 96), places=9)

    def test_distinct_measures(self):
        cs = walk(300)
        vals = {n: value(cs, n) for n in ("atr_14_pct", "ret_std_20", "realized_vol_20", "hl_range_pct")}
        self.assertEqual(len(set(vals.values())), 4)

    def test_flat_market_ratio_not_available(self):
        cs = [bar(i, 100, 100, 100, 100) for i in range(120)]
        f = E.snapshot(cs, M15, end_of(cs), ["vol_ratio_20_100"], symbol=BTC).features["vol_ratio_20_100"]
        self.assertEqual((f.status, f.value), (FeatureStatus.NOT_AVAILABLE, None))


class ReturnsTests(unittest.TestCase):
    def test_returns(self):
        cs = [bar(0, 100, 101, 99, 100), bar(1, 104, 106, 103, 105)]   # gap-up open
        self.assertAlmostEqual(value(cs, "ret_cc"), 0.05)
        self.assertAlmostEqual(value(cs, "log_ret"), math.log(1.05))
        self.assertAlmostEqual(value(cs, "ret_oc"), 105 / 104 - 1)
        self.assertEqual(value(cs, "hl_range"), 3.0)
        self.assertEqual(value(cs, "true_range"), 6.0)                  # uses previous close 100
        self.assertAlmostEqual(value(cs, "hl_range_pct"), 3 / 105)

    def test_rolling_return(self):
        cs = walk(50)
        self.assertAlmostEqual(value(cs, "ret_20"), float(cs[-1].close) / float(cs[-21].close) - 1, places=12)


class VolumeFeatureTests(unittest.TestCase):
    def test_raw_and_rolling(self):
        cs = walk(50)
        self.assertEqual(value(cs, "volume"), float(cs[-1].volume))
        self.assertEqual(value(cs, "turnover"), float(cs[-1].turnover))
        self.assertAlmostEqual(value(cs, "volume_sum_20"), sum(float(c.volume) for c in cs[-20:]), places=6)

    def test_relative_volume_excludes_current_bar(self):
        cs = [bar(i, 10, 11, 9, 10, v="2") for i in range(20)] + [bar(20, 10, 11, 9, 10, v="6")]
        self.assertEqual(value(cs, "rel_volume_20"), 3.0)

    def test_zero_baseline_not_available(self):
        cs = [bar(i, 10, 10, 10, 10, v="0", q="0") for i in range(20)] + [bar(20, 10, 11, 9, 10, v="5")]
        f = E.snapshot(cs, M15, end_of(cs), ["rel_volume_20"], symbol=BTC).features["rel_volume_20"]
        self.assertEqual(f.status, FeatureStatus.NOT_AVAILABLE)

    def test_unit_anomaly_detected(self):
        cs = [bar(i, 10, 11, 9, 10, v="2") for i in range(19)] + [bar(19, 10, 11, 9, 10, v="2", q="2000")]
        self.assertEqual(value(cs, "vt_anomaly_count_20"), 1.0)


def _const(value_):
    def fn(w):
        return value_
    return fn


class NanInfTests(unittest.TestCase):  # Tests 7, 8
    def _eval(self, fn):
        spec = FeatureSpec("probe", 1, "returns", "x", "probe", fn, 1, 1)
        eng = FeatureEngine(FeatureRegistry([spec]))
        return eng.snapshot(walk(5), M15, end_of(walk(5)), ["probe"], symbol=BTC).features["probe"]

    def test_nan_is_invalid(self):
        f = self._eval(_const(float("nan")))
        self.assertEqual((f.status, f.quality, f.value), (FeatureStatus.INVALID, QualityStatus.INVALID, None))

    def test_infinities_are_invalid(self):
        for x in (float("inf"), float("-inf")):
            with self.subTest(x=x):
                self.assertEqual(self._eval(_const(x)).status, FeatureStatus.INVALID)

    def test_math_errors_are_invalid_not_crash(self):
        def div0(w):
            return 1 / 0
        f = self._eval(div0)
        self.assertEqual(f.status, FeatureStatus.INVALID)
        self.assertIn("division", f.reason)

    def test_wrong_return_type_is_a_bug(self):
        with self.assertRaises(TypeError):
            self._eval(_const("1.0"))


class VersioningTests(unittest.TestCase):  # Test 13
    LOCK = Path(C.__file__).with_name("VERSIONS.lock")

    def test_lockfile_matches_formulas(self):
        lock = json.loads(self.LOCK.read_text())
        for s in R.all_versions():
            self.assertIn(s.key, lock, f"{s.key} not locked: run scripts/lock_feature_versions.py")
            self.assertEqual(lock[s.key], s.formula_hash,
                             f"{s.key}: formula changed without a version bump")

    def test_hash_sensitive_to_formula_params_and_helpers(self):
        base = FeatureSpec("x", 1, "trend", "quote", "d", C.f_sma, 20, 20, params={"n": 20})
        other_params = FeatureSpec("x", 1, "trend", "quote", "d", C.f_sma, 30, 30, params={"n": 30})
        other_fn = FeatureSpec("x", 1, "trend", "quote", "d", C.f_ema, 20, 80, params={"n": 20})
        self.assertEqual(len({base.formula_hash, other_params.formula_hash, other_fn.formula_hash}), 3)
        from app.research.features.spec import _formula_source
        self.assertIn("def ema_last", _formula_source(C.f_ema))       # helper is part of the hash

    def test_versions_distinguished_everywhere(self):
        v1 = R.get("sma_20")
        v2 = FeatureSpec("sma_20", 2, "trend", "quote", "d", C.f_sma, 20, 20, params={"n": 20})
        self.assertNotEqual(feature_set_hash([(M15, v1)]), feature_set_hash([(M15, v2)]))
        reg = FeatureRegistry(R.all_versions())
        reg.register(v2)
        f = FeatureEngine(reg).snapshot(walk(30), M15, end_of(walk(30)), ["sma_20"], symbol=BTC).features["sma_20"]
        self.assertEqual((f.version, f.key), (2, "sma_20:v2"))


class DeterminismTests(unittest.TestCase):  # Test 14
    def test_same_input_same_snapshot(self):
        cs = walk(1000)
        a = FeatureEngine().snapshot(cs, M15, end_of(cs), symbol=BTC)
        shuffled = list(cs)
        random.Random(3).shuffle(shuffled)
        b = FeatureEngine().snapshot(shuffled, M15, end_of(cs), symbol=BTC)
        self.assertEqual(a, b)
        self.assertEqual((a.feature_set_hash, a.input_fingerprint), (b.feature_set_hash, b.input_fingerprint))

    def test_fingerprint_changes_with_data(self):
        cs = walk(1000)
        cs2 = walk(1000, seed=8)
        a = E.snapshot(cs, M15, end_of(cs), ["ema_200"], symbol=BTC)
        b = E.snapshot(cs2, M15, end_of(cs2), ["ema_200"], symbol=BTC)
        self.assertNotEqual(a.input_fingerprint, b.input_fingerprint)


class FeatureValidationTests(unittest.TestCase):
    def test_spec_invariants(self):
        ok = dict(name="x", version=1, family="trend", unit="quote", description="d", fn=C.f_sma,
                  lookback=5, min_observations=5, params={"n": 5})
        FeatureSpec(**ok)
        for bad in ({"name": "EMA200"}, {"name": "ema-200"}, {"version": 0}, {"min_observations": 3},
                    {"fn": None}, {"is_proxy": True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                FeatureSpec(**{**ok, **bad})

    def test_catalog_requirements_consistent(self):
        for s in R.latest():
            if s.implemented:
                self.assertGreaterEqual(s.min_observations, s.lookback + s.warmup, s.name)
                self.assertEqual(s.max_gap_bars, 0, f"{s.name}: all v1 features are gap-strict")
                self.assertTrue(s.requires_closed)

    def test_feature_values_have_declared_unit_and_version(self):
        snap = E.snapshot(walk(900), M15, end_of(walk(900)), symbol=BTC)
        for key, f in snap.features.items():
            spec = R.get(f.name)
            self.assertEqual((f.unit, f.version, f.min_observations), (spec.unit, spec.version, spec.min_observations))


class CatalogDocTests(unittest.TestCase):
    def test_catalog_doc_in_sync_with_registry(self):
        doc = (Path(C.__file__).resolve().parents[3] / "docs" / "V070_FEATURE_CATALOG.md").read_text()
        for s in R.all_versions():
            self.assertIn(f"`{s.key}`", doc, "regenerate: python scripts/gen_feature_catalog.py")
            if s.implemented:
                self.assertIn(s.formula_hash, doc, f"{s.key} hash changed: regenerate the catalog")
