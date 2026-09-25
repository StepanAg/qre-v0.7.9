"""Regime v1 rules on hand-built FeatureSnapshots (exact thresholds, every branch)."""
import json
import unittest

from app.domain.enums import (FeatureStatus, MarketRegime, QualityStatus, TrendDirection, TrendStrength,
                              VolatilityState)
from app.domain.errors import DomainError
from app.research.regime.config import OPTIONAL_FEATURES, REQUIRED_FEATURES, RegimeConfig
from app.research.regime.rules import classify_timeframe
from tests.regime_data import CONFIG_FILE, M15, atr_units, cfg, feat, snap

C = cfg()


def classify(**kw):
    return classify_timeframe(snap(atr_units(**kw)), M15, C)


class RegimeClassificationTests(unittest.TestCase):
    def test_trending_up(self):
        r = classify(spread=2, slope=1, disp=1, ret=2)
        self.assertEqual((r.regime, r.trend_direction), (MarketRegime.TRENDING_UP, TrendDirection.UP))
        self.assertIn("REGIME:15m:trending_up", r.reason_codes)
        self.assertIn("STRUCTURE_UP:15m", r.reason_codes)

    def test_trending_down(self):
        r = classify(spread=-2, slope=-1, disp=-1, ret=-2)
        self.assertEqual((r.regime, r.trend_direction), (MarketRegime.TRENDING_DOWN, TrendDirection.DOWN))

    def test_ranging(self):
        r = classify(spread=0.3, slope=-0.2, disp=0.9)   # displacement alone does not make a trend
        self.assertEqual(r.regime, MarketRegime.RANGING)
        self.assertEqual(r.trend_strength, TrendStrength.NONE)

    def test_high_volatility_overrides_trend_but_keeps_direction(self):
        r = classify(spread=2, slope=1, disp=1, ret=2, vol_ratio=1.8)
        self.assertEqual((r.regime, r.volatility_state), (MarketRegime.HIGH_VOLATILITY, VolatilityState.HIGH))
        self.assertEqual(r.trend_direction, TrendDirection.UP)
        self.assertIn("HIGH_VOL_OVERRIDES_TREND:15m", r.reason_codes)

    def test_transition_conflict(self):
        r = classify(spread=2, slope=-1, disp=-1)       # structure up, momentum turned down
        self.assertEqual((r.regime, r.trend_direction), (MarketRegime.TRANSITION, TrendDirection.CONFLICT))
        self.assertIn("TRANSITION:15m:COMPONENTS_CONFLICT", r.reason_codes)

    def test_transition_incomplete(self):
        r = classify(spread=2, slope=1, disp=0.1)       # trend structure, price back at EMA50
        self.assertEqual((r.regime, r.trend_direction), (MarketRegime.TRANSITION, TrendDirection.UP))
        self.assertIn("TRANSITION:15m:COMPONENTS_INCOMPLETE", r.reason_codes)

    def test_horizon_veto(self):
        self.assertEqual(classify(spread=2, slope=1, disp=1, ret=-2).regime, MarketRegime.TRANSITION)

    def test_thresholds_are_inclusive_and_exact(self):
        c = C
        at = classify(spread=c.structure_min_atr, slope=c.slope_min_atr, disp=c.displacement_min_atr)
        below = classify(spread=c.structure_min_atr * 0.999, slope=c.slope_min_atr, disp=c.displacement_min_atr)
        self.assertEqual(at.regime, MarketRegime.TRENDING_UP)
        self.assertEqual(below.regime, MarketRegime.TRANSITION)
        self.assertEqual(classify(vol_ratio=c.vol_ratio_high).volatility_state, VolatilityState.HIGH)
        self.assertEqual(classify(vol_ratio=c.vol_ratio_low).volatility_state, VolatilityState.LOW)

    def test_strength_is_categorical_and_rule_based(self):
        strong = classify(spread=2, slope=C.strong_slope_atr, disp=1, ret=2)
        no_horizon = classify(spread=2, slope=C.strong_slope_atr, disp=1)
        moderate = classify(spread=2, slope=1, disp=1, ret=2)
        self.assertEqual(strong.trend_strength, TrendStrength.STRONG)
        self.assertEqual(no_horizon.trend_strength, TrendStrength.MODERATE)   # optional missing -> not STRONG
        self.assertEqual(moderate.trend_strength, TrendStrength.MODERATE)

    def test_metrics_explain_result(self):
        r = classify(spread=2, slope=1, disp=1, ret=2)
        self.assertAlmostEqual(r.metrics["spread_atr"], 2.0)
        self.assertAlmostEqual(r.metrics["slope_atr"], 1.0)
        self.assertEqual(set(r.metrics) >= {"spread_atr", "slope_atr", "displacement_atr", "vol_ratio", "atr_pct"},
                         True)


class RegimeDataQualityTests(unittest.TestCase):
    def _with(self, name, **kw):
        vals = atr_units(spread=2, slope=1, disp=1, ret=2)
        vals[name] = feat(name, None, **kw)
        return classify_timeframe(snap(vals), M15, C)

    def test_every_required_feature_blocks(self):
        for name in REQUIRED_FEATURES:
            with self.subTest(name=name):
                r = self._with(name, status=FeatureStatus.INSUFFICIENT_HISTORY,
                               quality=QualityStatus.INSUFFICIENT_HISTORY)
                self.assertEqual(r.regime, MarketRegime.UNKNOWN)
                self.assertEqual(r.data_quality, QualityStatus.INSUFFICIENT_HISTORY)
                self.assertTrue(any(name in c for c in r.reason_codes))
                self.assertEqual(r.metrics, {})          # nothing computed, nothing substituted

    def test_stale_and_gap_reported(self):
        stale = self._with("atr_14_pct", status=FeatureStatus.DATA_QUALITY_FAILURE, quality=QualityStatus.STALE)
        gap = self._with("ema_50_slope_10", status=FeatureStatus.DATA_QUALITY_FAILURE, quality=QualityStatus.GAP)
        self.assertEqual((stale.regime, stale.data_quality), (MarketRegime.UNKNOWN, QualityStatus.STALE))
        self.assertEqual((gap.regime, gap.data_quality), (MarketRegime.UNKNOWN, QualityStatus.GAP))
        self.assertIn("UNKNOWN:15m:DATA_QUALITY_STALE", stale.reason_codes)

    def test_worst_quality_wins(self):
        vals = atr_units(spread=2, slope=1, disp=1)
        vals["atr_14_pct"] = feat("atr_14_pct", None, status=FeatureStatus.INSUFFICIENT_HISTORY,
                                  quality=QualityStatus.INSUFFICIENT_HISTORY)
        vals["vol_ratio_20_100"] = feat("vol_ratio_20_100", None, status=FeatureStatus.DATA_QUALITY_FAILURE,
                                        quality=QualityStatus.STALE)
        self.assertEqual(classify_timeframe(snap(vals), M15, C).data_quality, QualityStatus.STALE)

    def test_missing_key_is_unknown_not_zero(self):
        vals = atr_units(spread=2, slope=1, disp=1)
        del vals["ema_50_200_spread"]
        r = classify_timeframe(snap(vals), M15, C)
        self.assertEqual(r.regime, MarketRegime.UNKNOWN)
        self.assertIn("REQ_FEATURE_MISSING:15m:ema_50_200_spread", r.reason_codes)

    def test_optional_missing_is_recorded_not_substituted(self):
        vals = atr_units(spread=2, slope=1, disp=1)
        vals["ret_20"] = feat("ret_20", None, status=FeatureStatus.DATA_QUALITY_FAILURE, quality=QualityStatus.GAP)
        r = classify_timeframe(snap(vals), M15, C)
        self.assertEqual(r.regime, MarketRegime.TRENDING_UP)
        self.assertIn("OPT_FEATURE_UNAVAILABLE:15m:ret_20:data_quality_failure", r.reason_codes)
        self.assertNotIn("return_atr", r.metrics)

    def test_zero_atr_is_unknown(self):
        r = classify(spread=0, slope=0, disp=0, atr_pct=0.0)
        self.assertEqual(r.regime, MarketRegime.UNKNOWN)
        self.assertTrue(any(c.endswith("ATR_ZERO") for c in r.reason_codes))

    def test_domain_refuses_regime_on_bad_data(self):
        from dataclasses import replace
        r = self._with("atr_14_pct", status=FeatureStatus.DATA_QUALITY_FAILURE, quality=QualityStatus.GAP)
        with self.assertRaises(DomainError):
            replace(r, regime=MarketRegime.TRENDING_UP)


class RegimeConfigTests(unittest.TestCase):
    def test_file_is_the_only_source(self):
        raw = json.loads(CONFIG_FILE.read_text())
        for k in [k for k in raw if not k.startswith("_") and k != "schema"]:
            broken = {x: y for x, y in raw.items() if x != k}
            with self.subTest(missing=k), self.assertRaises(DomainError):
                RegimeConfig.from_mapping(broken)
        with self.assertRaises(DomainError):
            RegimeConfig.from_mapping({**raw, "surprise": 1})

    def test_invalid_values_rejected(self):
        raw = json.loads(CONFIG_FILE.read_text())
        for bad in ({"vol_ratio_high": 0.9}, {"slope_min_atr": -1}, {"strong_slope_atr": 0.1},
                    {"timeframes": ["4h", "1h"]}, {"timeframes": ["15"]}, {"structure_min_atr": True}):
            with self.subTest(bad=bad), self.assertRaises(DomainError):
                RegimeConfig.from_mapping({**raw, **bad})

    def test_hash_tracks_thresholds(self):
        raw = json.loads(CONFIG_FILE.read_text())
        a = RegimeConfig.from_mapping(raw).config_hash
        b = RegimeConfig.from_mapping({**raw, "slope_min_atr": 0.6}).config_hash
        c = RegimeConfig.from_mapping({**raw, "_comment": "edited comment"}).config_hash
        self.assertNotEqual(a, b)
        self.assertEqual(a, c)

    def test_rules_read_only_phase2_features(self):
        from app.research.features.catalog import DEFAULT_REGISTRY
        for n in REQUIRED_FEATURES + OPTIONAL_FEATURES:
            spec = DEFAULT_REGISTRY.get(n)
            self.assertTrue(spec.implemented)
            self.assertFalse(spec.is_proxy, f"{n}: regime must not rely on proxies")
        self.assertNotIn("cvd", REQUIRED_FEATURES + OPTIONAL_FEATURES)

    def test_no_magic_numbers_in_rules(self):
        """Thresholds live in the JSON file: the rule module contains no float literals."""
        import ast
        from pathlib import Path
        src = Path(__file__).resolve().parents[1] / "app" / "research" / "regime" / "rules.py"
        floats = [n.value for n in ast.walk(ast.parse(src.read_text()))
                  if isinstance(n, ast.Constant) and isinstance(n.value, float)]
        self.assertEqual(floats, [])
