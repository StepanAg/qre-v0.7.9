#!/usr/bin/env python3
"""QRE unified test runner.

    python test.py            # human-readable
    python test.py --json     # machine-readable (stdout)
    python test.py --only "Domain models" --verbose

Statuses: PASS, FAIL, PARTIAL, NOT_IMPLEMENTED, SKIPPED, WARNING.
Exit code 1 if any critical category FAILs. Never needs network or real keys.
"""
from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import pkgutil
import sys
import time
import traceback
import unittest
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

PASS, FAIL, PARTIAL = "PASS", "FAIL", "PARTIAL"
NOT_IMPLEMENTED, SKIPPED, WARNING = "NOT_IMPLEMENTED", "SKIPPED", "WARNING"
STATUSES = (PASS, FAIL, PARTIAL, NOT_IMPLEMENTED, SKIPPED, WARNING)


@dataclass
class Result:
    category: str
    status: str
    detail: str = ""
    tests_run: int = 0
    failures: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    critical: bool = True
    notes: list[str] = field(default_factory=list)


@dataclass
class Category:
    name: str
    run: Callable[[], Result]
    critical: bool = True


# ------------------------------------------------------------------ helpers
def run_unittest(name: str, module: str) -> Result:
    """PASS only if tests actually ran and all passed. Zero tests = FAIL
    (an empty test file is not evidence of anything)."""
    try:
        suite = unittest.defaultTestLoader.loadTestsFromName(module)
    except Exception as e:  # import error in tests or code
        return Result(name, FAIL, f"cannot load {module}: {e}")
    stream = io.StringIO()
    res = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    fails = [f"{t.id()}: {tb.strip().splitlines()[-1]}" for t, tb in res.failures + res.errors]
    if res.testsRun == 0:
        return Result(name, FAIL, f"{module}: no tests collected")
    if len(res.skipped) == res.testsRun:
        return Result(name, SKIPPED, "all tests skipped", res.testsRun)
    if fails:
        return Result(name, FAIL, f"{len(fails)}/{res.testsRun} failed", res.testsRun, fails)
    detail = f"{res.testsRun} tests"
    if res.skipped:
        return Result(name, PARTIAL, f"{detail}, {len(res.skipped)} skipped", res.testsRun)
    return Result(name, PASS, detail, res.testsRun)


def unit(name: str, module: str, critical: bool = True) -> Category:
    """module may be 'tests.mod' or 'tests.mod.TestClass'."""
    return Category(name, lambda: run_unittest(name, module), critical)


def check_python() -> Result:
    v = sys.version_info
    if v < (3, 11):
        return Result("Python environment", FAIL, f"Python {v.major}.{v.minor} < 3.11")
    return Result("Python environment", PASS, f"Python {v.major}.{v.minor}.{v.micro}")


def check_imports() -> Result:
    import app

    errors, count = [], 0
    for m in pkgutil.walk_packages(app.__path__, "app."):
        if m.name == "app.__main__":
            continue
        count += 1
        try:
            importlib.import_module(m.name)
        except Exception as e:
            errors.append(f"{m.name}: {type(e).__name__}: {e}")
    if errors:
        return Result("Imports", FAIL, f"{len(errors)}/{count} modules failed", count, errors)
    return Result("Imports", PASS, f"{count} modules", count)


def check_version() -> Result:
    import app

    if app.__version__ != "0.7.0":
        return Result("Versioning", FAIL, f"expected 0.7.0, got {app.__version__}")
    toml = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if 'version = "0.7.0"' not in toml:
        return Result("Versioning", FAIL, "pyproject.toml version mismatch")
    return Result("Versioning", PASS, f"{app.__version__} -> target {app.TARGET_VERSION}")


def check_docs() -> Result:
    required = ["V070_DATA_QUALITY_POLICY.md", "V070_FEATURE_ENGINE.md", "V070_FEATURE_CATALOG.md",
                "V070_FEATURE_METHODOLOGY.md", "V070_PHASE3_READINESS.md", "V070_REGIME_ENGINE.md", "V070_PHASE4_READINESS.md", "V070_MARKET_MONITOR.md", "V070_PHASE5_READINESS.md", "V070_MARKET_STRUCTURE.md", "V070_PHASE6_READINESS.md", "V070_PHASE6_SETUP_DETECTION.md", "V070_PHASE7_READINESS.md", "V070_PHASE7_RESEARCH_BACKTEST.md", "V070_PHASE8_READINESS.md", "V070_PHASE8_ANALYTICS.md", "V070_PHASE9_READINESS.md", "V070_PHASE9_STRATEGY_RISK.md", "V070_PHASE10_READINESS.md", "V070_ARCHITECTURE.md", "V070_DOMAIN_MODEL.md", "V070_LESSONS_LEARNED.md",
                "V070_TEST_INFRASTRUCTURE.md", "V070_PHASE1_SPEC.md", "V070_MARKET_DATA.md",
                "V070_MARKET_DATA_SCHEMA.md", "V070_DATA_QUALITY.md", "V070_PHASE2_READINESS.md"]
    missing = [d for d in required if not (ROOT / "docs" / d).exists()]
    thin = [d for d in required if d not in missing and (ROOT / "docs" / d).stat().st_size < 1500]
    if missing:
        return Result("Documentation", FAIL, f"missing: {missing}", critical=False)
    if thin:
        return Result("Documentation", WARNING, f"suspiciously short: {thin}", critical=False)
    return Result("Documentation", PASS, f"{len(required)} documents")


def check_local_env() -> Result:
    """Non-critical: warns about a real .env that enables anything risky."""
    env = ROOT / ".env"
    if not env.exists():
        return Result("Local .env", SKIPPED, "no .env (defaults used)", critical=False)
    from app.config.settings import load_settings

    try:
        load_settings(environ=None)
    except Exception as e:
        return Result("Local .env", FAIL, f"{type(e).__name__}: {e}")
    return Result("Local .env", PASS, ".env loads and passes safety validation")


def self_test() -> Result:
    """The runner must not produce fake PASS: feed it synthetic suites."""
    name = "Test infrastructure"
    import types

    probe = types.ModuleType("_qre_probe")

    class Failing(unittest.TestCase):
        def test_x(self):
            self.fail("boom")

    class Passing(unittest.TestCase):
        def test_x(self):
            pass

    class Skipping(unittest.TestCase):
        @unittest.skip("n/a")
        def test_x(self):
            pass

    checks = []
    for cls, expected in ((Failing, FAIL), (Passing, PASS), (Skipping, SKIPPED)):
        probe.__dict__.clear()
        probe.Case = cls
        sys.modules["_qre_probe"] = probe
        checks.append((cls.__name__, run_unittest("probe", "_qre_probe").status, expected))
    empty = types.ModuleType("_qre_empty")
    sys.modules["_qre_empty"] = empty
    checks.append(("Empty", run_unittest("probe", "_qre_empty").status, FAIL))
    sys.modules.pop("_qre_probe", None)
    sys.modules.pop("_qre_empty", None)
    bad = [f"{n}: got {g}, expected {e}" for n, g, e in checks if g != e]
    if bad:
        return Result(name, FAIL, "runner misclassifies results", len(checks), bad)
    return Result(name, PASS, f"{len(checks)} runner self-checks", len(checks))


NETWORK_MODE = False


def structure_baseline() -> Result:
    """Deterministic synthetic structure baseline (in memory, no DB, no network).
    Counts are SYNTHETIC: they validate the engine, they are not market or trading statistics."""
    name = "Structure Offline Baseline"
    from app.domain.market import Timeframe
    from app.research.structure.service import StructureService
    from tests.structure_data import ETH, M15, PATH_A, PATH_B, T0, bar_close, candles, cfg, mirror, random_walk

    svc = StructureService(None, cfg())
    sets = [("hand PATH_A", candles(PATH_A), bar_close(18)), ("hand PATH_B", candles(PATH_B), bar_close(10)),
            ("hand mirror(PATH_A)", candles(mirror(PATH_A)), bar_close(18))]
    for seed in (11, 12):
        sets.append((f"random walk seed={seed} 15m x3000", random_walk(3000, seed), T0 + M15.delta * 3000))
        h1 = random_walk(1200, seed, tf=Timeframe.H1)
        sets.append((f"random walk seed={seed} 1h x1200", h1, T0 + Timeframe.H1.delta * 1200))
    notes = ["SYNTHETIC datasets - counts validate the engine, they are NOT market or trading statistics"]
    failures = []
    for label, cs, as_of in sets:
        a = svc.compute(cs, ETH, cs[0].timeframe, as_of)
        b = svc.compute(list(reversed(cs)), ETH, cs[0].timeframe, as_of)
        if a.to_json() != b.to_json():
            failures.append(f"{label}: non-deterministic")
        ev = {t.value: sum(1 for e in a.events if e.event_type is t) for t in type(a.events[0].event_type)} \
            if a.events else {}
        notes.append(f"{label}: quality={a.data_quality.value} direction={a.direction.value} "
                     f"swings={len(a.swing_highs)}H/{len(a.swing_lows)}L (shown, max {svc.config.snapshot_max_swings}) "
                     f"events(last {svc.config.recent_events})={ev or 0} EQH={len(a.equal_highs)} "
                     f"EQL={len(a.equal_lows)} active_liquidity={len(a.liquidity)} recent_sweeps={len(a.sweeps)}")
    # bar-by-bar replay: every event recorded exactly once, identical to the final snapshot's view
    cs = random_walk(900, 21)
    seen: dict[str, str] = {}
    for k in range(700, 901):
        s = svc.compute(cs[:k], ETH, M15, T0 + M15.delta * k)
        for e in s.events:
            prev = seen.setdefault(e.event_id, e.event_type.value)
            if prev != e.event_type.value:
                failures.append(f"replay: event {e.event_id} relabelled {prev} -> {e.event_type.value}")
    notes.append(f"replay random walk seed=21, 201 decision points: {len(seen)} distinct break events, "
                 f"relabelled={sum(1 for f in failures if 'relabelled' in f)}")
    hand = svc.compute(candles(PATH_A), ETH, M15, bar_close(18))
    if [e.event_type.value for e in hand.events] != ["break_unclassified", "bos", "choch"]:
        failures.append("hand PATH_A: expected unclassified, bos, choch")
    if failures:
        return Result(name, FAIL, f"{len(failures)} problems", len(sets), failures, notes=notes)
    return Result(name, PASS, f"{len(sets)} datasets + 201-step replay (synthetic)", len(sets), notes=notes)


def data_quality_metrics() -> Result:
    """Coverage on a deterministic dataset with a real gap, a trailing open
    candle and a short-history start. Coverage is always shown WITH its
    exclusions; the check fails if exclusions do not add up to 100 %."""
    name = "Data Quality Metrics"
    from dataclasses import replace

    from app.research.engine import FeatureEngine
    from tests.feature_data import walk

    candles = walk(1500, missing={1100, 1101})
    forming = replace(candles[-1], open_time=candles[-1].close_time, is_closed=False)
    names = ["ema_50", "ema_200", "atr_14", "ret_std_20", "rel_volume_20",
             "clv_volume_pressure_proxy_20", "cvd"]
    rep = FeatureEngine().coverage(candles + [forming], candles[0].timeframe, names)
    st = rep.series_stats
    tot = rep.totals()
    notes = [f"dataset: {rep.points} decision points ({rep.timeframe.value}), synthetic, offline",
             f"valid observations={st.valid_observations} missing observations={st.missing_observations} "
             f"gaps={st.gaps} invalid observations={st.invalid_observations} "
             f"open-candle exclusions={st.open_candle_exclusions}",
             f"insufficient-history events={tot.get('insufficient_history', 0)} "
             f"feature calculation failures="
             f"{sum(v for k, v in tot.items() if k.startswith('data_quality_failure') or k == 'invalid')} "
             f"not-available={tot.get('not_available', 0)}"]
    notes += [rep.explain(n) for n in names]
    bad = [n for n in names if sum(rep.per_feature[n].values()) != rep.points]
    if bad:
        return Result(name, FAIL, f"exclusions do not add up for {bad}", failures=bad, notes=notes)
    return Result(name, PASS, f"{len(names)} features x {rep.points} points", len(names), notes=notes)


def network_smoke() -> Result:
    """Real Bybit public API. SKIPPED unless --network; then the guard is lifted
    for this category only and a FAIL counts."""
    name = "Bybit Network Smoke"
    if not NETWORK_MODE:
        return Result(name, SKIPPED, "offline baseline (enable with --network)", critical=False)
    from tests import netguard
    os.environ["QRE_NETWORK_TESTS"] = "1"
    netguard.uninstall()
    try:
        r = run_unittest(name, "tests.test_md_network")
    finally:
        netguard.install()
    r.critical = True
    return r


def component(name: str) -> Category:
    """NOT_IMPLEMENTED unless registered with a real implementation AND tests."""

    def run() -> Result:
        from app.core import registry

        info = registry.get(name)
        if info.implementation is None:
            return Result(name, NOT_IMPLEMENTED, f"planned for phase {info.phase}", critical=False)
        mod, _, cls = info.implementation.partition(":")
        try:
            getattr(importlib.import_module(mod), cls)
        except Exception as e:
            return Result(name, FAIL, f"registered implementation missing: {e}")
        if not info.test_module:
            return Result(name, PARTIAL, "implementation without tests", critical=False)
        return run_unittest(name, info.test_module)

    return Category(name, run, critical=True)


CATEGORIES: list[Category] = [
    Category("Python environment", check_python),
    Category("Imports", check_imports),
    Category("Versioning", check_version),
    unit("Configuration", "tests.test_config"),
    unit("Domain models", "tests.test_domain"),
    unit("Accounting invariants", "tests.test_accounting"),
    unit("Database bootstrap", "tests.test_storage"),
    unit("Architecture Rules", "tests.test_architecture.ArchitectureTests"),
    Category("Test infrastructure", self_test),
    unit("Safety configuration", "tests.test_safety"),
    unit("Logging", "tests.test_logging"),
    unit("CLI", "tests.test_cli"),
    # ---- Phase 1: market data (offline; fixtures + synthetic exchange)
    unit("Market Data Imports", "tests.test_md_parsing.MarketDataImportTests"),
    unit("Provider Interface", "tests.test_md_parsing.ProviderInterfaceTests"),
    unit("Bybit Response Parsing", "tests.test_md_parsing.ResponseParsingTests"),
    unit("Normalization", "tests.test_md_parsing.NormalizationTests"),
    unit("Timestamp Validation", "tests.test_md_parsing.TimestampTests"),
    unit("Candle Invariants", "tests.test_md_parsing.CandleInvariantTests"),
    unit("Fixtures", "tests.test_md_parsing.FixtureTests"),
    unit("Failure Handling", "tests.test_md_client.FailureHandlingTests"),
    unit("Rate Limits & Retry", "tests.test_md_client.RateLimitRetryTests"),
    unit("Duplicate Detection", "tests.test_md_quality.DuplicateDetectionTests"),
    unit("Gap Detection", "tests.test_md_quality.GapDetectionTests"),
    unit("Pagination", "tests.test_md_backfill.PaginationTests"),
    unit("Backfill", "tests.test_md_backfill.BackfillTests"),
    unit("Idempotency", "tests.test_md_backfill.IdempotencyTests"),
    unit("Warmup", "tests.test_md_backfill.WarmupTests"),
    unit("SQLite Persistence", "tests.test_md_storage.SQLitePersistenceTests"),
    unit("Migration", "tests.test_md_storage.MigrationTests"),
    unit("Single Writer", "tests.test_md_storage.SingleWriterTests"),
    unit("Network Isolation", "tests.test_architecture.NetworkIsolationTests"),
    Category("Bybit Network Smoke", lambda: network_smoke(), critical=False),
    # ---- Phase 2: data quality policy & feature engine
    unit("Data Quality Policy", "tests.test_features_quality.DataQualityPolicyTests"),
    unit("Gap Classification", "tests.test_features_quality.GapClassificationTests"),
    unit("No Silent Fill", "tests.test_features_quality.NoSilentFillTests"),
    unit("Insufficient History", "tests.test_features_quality.InsufficientHistoryTests"),
    unit("Closed Candle Protection", "tests.test_features_quality.ClosedCandleTests"),
    unit("Feature Registry", "tests.test_features_calc.FeatureRegistryTests"),
    unit("Feature Catalog Sync", "tests.test_features_calc.CatalogDocTests"),
    unit("Feature Validation", "tests.test_features_calc.FeatureValidationTests"),
    unit("CVD Naming", "tests.test_features_calc.CVDNamingTests"),
    unit("EMA", "tests.test_features_calc.EMATests"),
    unit("SMA", "tests.test_features_calc.SMATests"),
    unit("ATR", "tests.test_features_calc.ATRTests"),
    unit("Volatility", "tests.test_features_calc.VolatilityTests"),
    unit("Returns", "tests.test_features_calc.ReturnsTests"),
    unit("Volume Features", "tests.test_features_calc.VolumeFeatureTests"),
    unit("NaN/Inf Protection", "tests.test_features_calc.NanInfTests"),
    unit("Feature Versioning", "tests.test_features_calc.VersioningTests"),
    unit("Feature Determinism", "tests.test_features_calc.DeterminismTests"),
    unit("Look-ahead Protection", "tests.test_features_integrity.LookAheadTests"),
    unit("Multi-Timeframe Alignment", "tests.test_features_integrity.MTFAlignmentTests"),
    unit("Feature Persistence", "tests.test_features_integrity.FeaturePersistenceTests"),
    unit("Feature Cache", "tests.test_features_integrity.FeatureServiceCacheTests"),
    unit("Research Safety", "tests.test_features_integrity.ResearchSafetyTests"),
    Category("Data Quality Metrics", lambda: data_quality_metrics()),
    # ---- Phase 3: regime engine
    unit("Regime Classification", "tests.test_regime_rules.RegimeClassificationTests"),
    unit("Regime Data Quality", "tests.test_regime_rules.RegimeDataQualityTests"),
    unit("Regime Config", "tests.test_regime_rules.RegimeConfigTests"),
    unit("Regime Scenarios", "tests.test_regime_integration.ScenarioTests"),
    unit("Regime Data Failures", "tests.test_regime_integration.RegimeDataFailureTests"),
    unit("Regime MTF", "tests.test_regime_integration.MultiTimeframeTests"),
    unit("Regime Look-ahead", "tests.test_regime_integration.RegimeLookAheadTests"),
    unit("BTC Context", "tests.test_regime_integration.BtcContextTests"),
    unit("Regime Determinism", "tests.test_regime_integration.RegimeDeterminismTests"),
    unit("Regime Persistence", "tests.test_regime_integration.RegimePersistenceTests"),
    unit("Regime CLI", "tests.test_regime_integration.RegimeCliTests"),
    unit("Regime Safety", "tests.test_regime_integration.RegimeSafetyTests"),
    # ---- Phase 5: market structure & liquidity (offline, hand-built + seeded synthetic data)
    unit("Swing Detection", "tests.test_structure.SwingDetectionTests"),
    unit("BOS Detection", "tests.test_structure.BosTests"),
    unit("CHoCH Detection", "tests.test_structure.ChochTests"),
    unit("EQH / EQL", "tests.test_structure.EqualLevelTests"),
    unit("Liquidity Levels", "tests.test_structure.LiquidityTests"),
    unit("Sweep Detection", "tests.test_structure.SweepTests"),
    unit("Structure No-Look-Ahead", "tests.test_structure.NoLookAheadTests"),
    unit("Structure Closed Candles", "tests.test_structure.ClosedCandleTests"),
    unit("Structure Gap Handling", "tests.test_structure.GapTests"),
    unit("Structure Data Quality", "tests.test_structure.InsufficientAndInvalidDataTests"),
    unit("Structure Determinism", "tests.test_structure.DeterminismTests"),
    unit("Structure Persistence", "tests.test_structure.PersistenceTests"),
    unit("Structure Config", "tests.test_structure.StructureConfigTests"),
    unit("Structure MTF Alignment", "tests.test_structure.MtfAlignmentTests"),
    unit("Structure Safety", "tests.test_structure.StructureSafetyTests"),
    unit("Structure Monitor Integration", "tests.test_structure_integration.MonitorStructureTests"),
    unit("Structure CLI", "tests.test_structure_integration.StructureCliTests"),
    Category("Structure Offline Baseline", lambda: structure_baseline()),
    # ---- Phase 6: setup detection (offline, hand-built fixtures)
    unit("Setup Breakout", "tests.test_setup.BreakoutTests"),
    unit("Setup Pullback", "tests.test_setup.PullbackTests"),
    unit("Setup Sweep Reversal", "tests.test_setup.SweepReversalTests"),
    unit("Setup Range Rejection", "tests.test_setup.RangeRejectionTests"),
    unit("Setup Direction/Data/Regime", "tests.test_setup.DirectionDataAndRegimeTests"),
    unit("Setup Point-in-Time", "tests.test_setup.PointInTimeTests"),
    unit("Setup Determinism", "tests.test_setup.DeterminismTests"),
    unit("Setup Config", "tests.test_setup.SetupConfigTests"),
    unit("Setup Persistence", "tests.test_setup.SetupPersistenceTests"),
    unit("Setup Safety", "tests.test_setup.SetupSafetyTests"),
    unit("Setup + Regime/Structure", "tests.test_setup_integration.SetupWithRegimeAndStructureTests"),
    unit("Setup Monitor Integration", "tests.test_setup_integration.MonitorSetupTests"),
    unit("Setup CLI", "tests.test_setup_integration.SetupCliTests"),
    # ---- Phase 7: research & backtest infrastructure (offline, hand-computed fixtures)
    unit("Research Dataset", "tests.test_research.DatasetTests"),
    unit("Research Replay", "tests.test_research.ReplayTests"),
    unit("Setup Outcomes", "tests.test_research.OutcomeTests"),
    unit("Backtest Engine", "tests.test_research.BacktestTests"),
    unit("Time Splits", "tests.test_research.SplitTests"),
    unit("Research Persistence", "tests.test_research.ResearchPersistenceTests"),
    unit("Research Lab Safety", "tests.test_research.ResearchSafetyTests"),
    unit("Research CLI", "tests.test_research_integration.ResearchCliTests"),
    unit("Research + Regime", "tests.test_research_integration.ResearchWithRegimeTests"),
    # ---- Phase 8: analytics (read-only, hand-computed fixtures)
    unit("Analytics Read-only Foundation", "tests.test_analytics.ReadOnlyFoundationTests"),
    unit("Analytics Distributions", "tests.test_analytics.DistributionTests"),
    unit("Analytics Performance/Risk/Costs", "tests.test_analytics.PerformanceRiskCostTests"),
    unit("Analytics Drawdown", "tests.test_analytics.DrawdownTests"),
    unit("Analytics Excursions/Exits", "tests.test_analytics.ExcursionExitTests"),
    unit("Analytics Groups/Portfolio/Periods", "tests.test_analytics.GroupingPortfolioPeriodTests"),
    unit("Analytics Funnels/Untraded", "tests.test_analytics.FunnelUntradedTests"),
    unit("Analytics Funnel on Real Run", "tests.test_analytics.FunnelOnRealBacktestTests"),
    unit("Analytics Versions/Compare", "tests.test_analytics.VersionCompareTests"),
    unit("Analytics Data Quality", "tests.test_analytics.DataQualityMonitorTests"),
    unit("Analytics CLI", "tests.test_analytics_cli.AnalyticsCliTests"),
    # ---- Phase 9: Strategy + Risk Engine (offline, hand-computed fixtures)
    unit("Phase 9 Contracts", "tests.test_phase9.ContractTests"),
    unit("Strategy v1", "tests.test_phase9.StrategyTests"),
    unit("Risk Engine v1", "tests.test_phase9.RiskEngineTests"),
    unit("Decision Backtest Loop", "tests.test_phase9.DecisionLoopTests"),
    unit("Equivalence with Phase 7", "tests.test_phase9.EquivalenceTests"),
    unit("Phase 7 Compatibility", "tests.test_phase9.Phase7CompatibilityTests"),
    unit("Strategy+Risk Pipeline", "tests.test_phase9.PipelineRunTests"),
    unit("Strategy+Risk Analytics", "tests.test_phase9.AnalyticsFunnelTests"),
    unit("Strategy+Risk CLI", "tests.test_phase9.Phase9CliTests"),
    unit("Legacy Contract Retirement", "tests.test_phase9.LegacyRetirementTests"),
    # ---- Phase 4: market monitor runtime (offline: synthetic exchange + fake time)
    unit("Monitor Cycle", "tests.test_monitor.MonitorCycleTests"),
    unit("Monitor Data Conditions", "tests.test_monitor.MonitorDataConditionTests"),
    unit("Monitor Retry & Recovery", "tests.test_monitor.MonitorRetryTests"),
    unit("Monitor Idempotency", "tests.test_monitor.MonitorIdempotencyTests"),
    unit("Monitor Lifecycle", "tests.test_monitor.MonitorLifecycleTests"),
    unit("Monitor CLI", "tests.test_monitor.MonitorCliTests"),
    unit("Monitor Config", "tests.test_monitor.MonitorConfigTests"),
    unit("Monitor Safety", "tests.test_monitor.MonitorSafetyTests"),
    unit("Supervisor (watchdog)", "tests.test_supervisor.SupervisorTests"),
    unit("Run State & Logs", "tests.test_supervisor.HeartbeatAndRunStateTests"),
    unit("Outage & Sleep Resilience", "tests.test_supervisor.MonitorResilienceTests"),
    Category("Documentation", check_docs, critical=False),
    Category("Local .env", check_local_env),  # unsafe .env must block
    component("Market Data"),
    component("Research / Features"),
    component("Regime Engine"),
    component("Market Monitor"),
    component("Market Structure"),
    component("Setup Detection"),
    component("Analytics"),
    component("Strategy"),
    component("Risk Engine"),
    component("Backtest"),
    component("Execution"),
    component("Reconciliation"),
    component("AI"),
]


def run_all(only: list[str] | None) -> list[Result]:
    results = []
    for cat in CATEGORIES:
        if only and cat.name not in only:
            continue
        t0 = time.perf_counter()
        try:
            r = cat.run()
        except Exception as e:
            r = Result(cat.name, FAIL, f"crashed: {type(e).__name__}: {e}",
                       failures=[traceback.format_exc()])
        r.duration_s = round(time.perf_counter() - t0, 3)
        r.critical = (cat.critical and r.critical) or (cat.name == "Bybit Network Smoke" and r.critical
                                                        and NETWORK_MODE)
        results.append(r)
    return results


def overall(results: list[Result]) -> str:
    if any(r.status == FAIL for r in results):
        return FAIL
    if any(r.status in (PARTIAL, WARNING) for r in results):
        return PARTIAL
    return PASS


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="print JSON report")
    ap.add_argument("--json-file", type=Path, help="also write JSON report to file")
    ap.add_argument("--only", action="append", help="run only this category (repeatable)")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--network", action="store_true",
                    help="also run the real Bybit public API smoke test (not part of the baseline)")
    args = ap.parse_args()

    import logging

    global NETWORK_MODE
    NETWORK_MODE = args.network
    os.environ["QRE_TEST_RUNNER"] = "1"
    os.environ.pop("QRE_NETWORK_TESTS", None)
    logging.getLogger("qre").addHandler(logging.NullHandler())
    logging.getLogger("qre").propagate = False
    from tests import netguard
    netguard.install()          # the whole baseline runs with sockets/DNS blocked

    import app

    t0 = time.perf_counter()
    results = run_all(args.only)
    total = round(time.perf_counter() - t0, 3)
    status = overall(results)
    critical_fail = any(r.status == FAIL and r.critical for r in results)
    report = {
        "network_mode": NETWORK_MODE,
        "project": "qre", "version": app.__version__, "phase": app.CURRENT_PHASE,
        "overall": status, "critical_fail": critical_fail, "duration_s": total,
        "counts": {s: sum(1 for r in results if r.status == s) for s in STATUSES},
        "results": [asdict(r) for r in results],
    }
    if args.json_file:
        args.json_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"QRE {app.__version__} — phase {app.CURRENT_PHASE} test run\n")
        for r in results:
            tag = f"[{r.status}]"
            print(f"{tag:18} {r.category:24} {r.detail:40} {r.duration_s:6.3f}s")
            if r.failures and (args.verbose or r.status == FAIL):
                for f in r.failures:
                    print(f"{'':19}- {f}")
            for n in r.notes:
                print(f"{'':19}| {n}")
        print("\n" + "  ".join(f"{k}={v}" for k, v in report["counts"].items()))
        print(f"OVERALL: {status}   ({total:.2f}s)")
    return 1 if critical_fail else 0


if __name__ == "__main__":
    sys.exit(main())
