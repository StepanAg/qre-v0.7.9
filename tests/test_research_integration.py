"""Phase 7 through the CLI and with the regime service (offline)."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.research_data import BCFG, M15, Lab, bcfg


class ResearchCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = Lab()
        cls.env = {"DB_PATH": str(cls.lab.db.path)}

    @classmethod
    def tearDownClass(cls):
        cls.lab.close()

    def cli(self, *argv):
        from app.cli.main import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, self.env):
            code = main(list(argv))
        return code, out.getvalue()

    def test_full_cycle(self):
        s, e = self.lab.start.isoformat(), self.lab.end.isoformat()
        code, out = self.cli("research", "dataset", "build", "--symbols", "ETHUSDT", "--tf", "15m", "--start", s,
                             "--end", e)
        self.assertEqual(code, 0)
        ds = out.split()[1]
        self.assertTrue(ds.startswith("ds_"))
        code, out = self.cli("research", "dataset", "show", "--id", ds)
        self.assertEqual(json.loads(out)["dataset_id"], ds)
        code, out = self.cli("research", "replay", "--dataset", ds)
        rp = out.split()[1]
        self.assertIn('"observations": 3', out)
        code, out = self.cli("research", "outcomes", "--replay", rp)
        self.assertIn("setups are not trades", out)
        cfg = Path(tempfile.mkdtemp()) / "bt.json"
        cfg.write_text(json.dumps({**json.loads(BCFG.read_text()), **bcfg().canonical(), "_comment": "test"}))
        code, out = self.cli("research", "backtest", "--replay", rp, "--config", str(cfg))
        self.assertIn("RESEARCH / SIMULATION", out)
        self.assertIn("trades=1", out)
        bt = out.split("backtest ")[1].split()[0]
        code, out = self.cli("research", "metrics", "--id", bt)
        self.assertIn("net_pnl", out)
        self.assertIn("profit_factor              n/a (no_losing_trades)", out)
        code, out = self.cli("research", "run", "--id", bt)
        self.assertEqual(json.loads(out)["kind"], "backtest")
        exp = Path(tempfile.mkdtemp()) / "r.json"
        code, out = self.cli("research", "export", "--id", bt, "--out", str(exp))
        doc = json.loads(exp.read_text())
        self.assertEqual(len(doc["trades"]), 1)
        self.assertTrue(doc["limitations"])
        code, out = self.cli("research", "status")
        self.assertEqual(json.loads(out)["counts"]["sim_trades"], 1)

    def test_clear_errors(self):
        code, _ = self.cli("research", "run", "--id", "bt_missing")
        self.assertEqual(code, 1)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = self.cli("research", "replay", "--dataset", "ds_missing")
        self.assertEqual(code, 2)


class ResearchWithRegimeTests(unittest.TestCase):
    def test_regime_replay_on_context_dataset(self):
        from app.research.lab.pipeline import ResearchPipeline
        from app.research.regime.config import RegimeConfig
        from app.research.regime.service import RegimeService
        from app.research.service import FeatureService
        from tests.regime_data import CONFIG_FILE
        lab = Lab()
        try:
            regime = RegimeService(FeatureService(lab.db.store), RegimeConfig.from_file(CONFIG_FILE))
            pipe = ResearchPipeline(lab.db.store, lab.pipe.setups, lab.pipe.cfg, lab.store, regime)
            ds, _ = pipe.build_dataset(["ETHUSDT"], M15, lab.start, lab.end, with_context=True)
            roles = sorted({(c.symbol, c.timeframe, c.role) for c in ds.coverages})
            self.assertIn(("BTCUSDT", "1h", "context"), roles)
            self.assertIn("regime", ds.config_hashes)
            rp, _ = pipe.replay(ds.dataset_id, with_regime=True)
            plain, _ = pipe.replay(ds.dataset_id, with_regime=False)
            self.assertNotEqual(rp.run_id, plain.run_id)
            self.assertTrue(rp.with_regime)
            # no 1h/4h/BTC candles stored -> regime honestly unknown -> fit unknown (never invented)
            self.assertEqual({o.first_regime_fit for o in rp.observations}, {"unknown"})
        finally:
            lab.close()

    def test_zero_cost_backtest_is_flagged(self):
        lab = Lab()
        try:
            rp = lab.replay()
            zero = {"taker_fee_bps": 0, "maker_fee_bps": 0, "slippage_bps": 0, "funding_mode": "none",
                    "assumed_funding_rate_8h": 0.0001}
            _, rep, _ = lab.pipe.backtest(rp.run_id, bcfg(costs=zero))
            self.assertIn("ZERO_COST_MODEL: results exclude all execution costs", rep["summary"]["warnings"])
            self.assertTrue(rep["summary"]["limitations"])
            self.assertEqual(rep["summary"]["cost_model"]["funding_mode"], "none")
        finally:
            lab.close()
