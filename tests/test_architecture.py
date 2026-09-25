"""Static dependency rules between layers (AST based, no imports executed)."""
import ast
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

# layer -> app layers it must NOT import
FORBIDDEN = {
    "domain": {"core", "config", "storage", "data", "research", "strategy", "risk",
               "execution", "accounting", "analytics", "ai", "cli"},
    "core": {"config", "domain", "storage", "data", "research", "strategy", "risk",
             "execution", "accounting", "analytics", "ai", "cli"},
    "config": {"storage", "data", "research", "strategy", "risk", "execution",
               "accounting", "analytics", "ai", "cli"},
    "data": {"strategy", "risk", "execution", "accounting", "analytics", "ai", "cli"},
    "research": {"storage", "strategy", "risk", "execution", "accounting", "ai", "cli"},
    "strategy": {"storage", "execution", "accounting", "ai", "cli", "config"},
    "risk": {"storage", "execution", "accounting", "ai", "cli"},
    "execution": {"strategy", "accounting", "analytics", "ai", "cli", "research"},
    "accounting": {"execution", "strategy", "ai", "cli", "data"},
    "analytics": {"execution", "strategy", "ai", "cli", "storage"},
    "ai": {"storage", "execution", "accounting", "data", "cli", "config"},
    "storage": {"strategy", "execution", "ai", "cli", "research", "risk", "data", "monitor"},
    # Phase 4: runtime orchestration - reuses data/research through their services,
    # never execution/strategy/risk/AI, never concrete storage or the network layer.
    "monitor": {"execution", "strategy", "risk", "ai", "accounting", "analytics", "cli", "storage", "config"},
}
NETWORK_LIBS = {"requests", "httpx", "aiohttp", "urllib.request", "urllib.error", "http.client", "socket",
                "ssl", "websocket", "websockets", "pybit", "ccxt"}
# Phase 1: the single module allowed to touch the network.
NETWORK_ALLOWED = {"app/data/http.py"}
# Modules that constitute the network adapter; only data/ and cli/ may import them.
NETWORK_LAYER = ("app.data.http", "app.data.bybit")
MAY_IMPORT_NETWORK_LAYER = {"data", "cli"}


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


class ArchitectureTests(unittest.TestCase):
    def test_layer_dependencies(self):
        violations = []
        for layer, forbidden in FORBIDDEN.items():
            for f in (APP / layer).rglob("*.py"):
                for imp in imports_of(f):
                    parts = imp.split(".")
                    if parts[0] == "app" and len(parts) > 1 and parts[1] in forbidden:
                        violations.append(f"{f.relative_to(APP.parent)} -> {imp}")
        self.assertEqual(violations, [], "\n".join(violations))

    def test_network_libs_only_in_http_module(self):
        bad = []
        for f in APP.rglob("*.py"):
            rel = f.relative_to(APP.parent).as_posix()
            for imp in imports_of(f):
                if any(imp == n or imp.startswith(n + ".") for n in NETWORK_LIBS) and rel not in NETWORK_ALLOWED:
                    bad.append(f"{rel} -> {imp}")
        self.assertEqual(bad, [], "network libraries are allowed only in app/data/http.py")

    def test_network_layer_not_imported_by_core_layers(self):  # Test 11
        bad = []
        for f in APP.rglob("*.py"):
            layer = f.relative_to(APP).parts[0]
            if layer in MAY_IMPORT_NETWORK_LAYER or f.parent == APP:
                continue
            for imp in imports_of(f):
                if imp.startswith(NETWORK_LAYER):
                    bad.append(f"{f.relative_to(APP.parent)} -> {imp}")
        self.assertEqual(bad, [])

    def test_rule_is_real(self):
        """Self-check: the scanner does detect a forbidden import."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as t:
            t.write("import requests\nfrom app.data.bybit.client import BybitRestClient\n")
        imps = imports_of(Path(t.name))
        Path(t.name).unlink()
        self.assertIn("requests", imps)
        self.assertTrue(any(i.startswith(NETWORK_LAYER) for i in imps))

    def test_sql_only_in_storage(self):
        bad = []
        for f in APP.rglob("*.py"):
            if "storage" in f.parts:
                continue
            if "sqlite3" in imports_of(f):
                bad.append(str(f.relative_to(APP.parent)))
        self.assertEqual(bad, [])

    def test_analytics_reader_is_read_only(self):
        """Phase 8: the analytics read path opens SQLite with mode=ro and contains no
        statement other than SELECT; the analytics package never touches sqlite3."""
        import re
        src = (APP / "storage" / "analytics_read.py").read_text()
        self.assertIn("mode=ro", src)
        sql = [c.value for c in ast.walk(ast.parse(src)) if isinstance(c, ast.Constant) and isinstance(c.value, str)
               and re.match(r"\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE|PRAGMA|ATTACH|VACUUM)\b",
                            c.value, re.I)]
        self.assertTrue(sql, "no SQL found - the rule would be vacuous")
        self.assertEqual([q for q in sql if not re.match(r"\s*SELECT\b", q, re.I)], [])
        self.assertIsNone(re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|ATTACH|VACUUM)\b\s", src))
        for f in (APP / "analytics").rglob("*.py"):
            names = {a.name for n in ast.walk(ast.parse(f.read_text())) if isinstance(n, (ast.Import, ast.ImportFrom))
                     for a in n.names} | {n.module or "" for n in ast.walk(ast.parse(f.read_text()))
                                          if isinstance(n, ast.ImportFrom)}
            self.assertFalse({"sqlite3", "app.storage"} & names, f.name)

    def test_strategy_and_risk_are_pure(self):
        """Phase 9: deterministic Python only - no clock, randomness, network, storage, LLM or execution;
        RiskSettings now has a consumer (finding F3)."""
        banned_calls = {"now", "utcnow", "today", "time", "perf_counter", "random", "randint", "choice", "urlopen"}
        banned_names = {"random", "sqlite3", "socket", "requests", "anthropic", "openai", "gemini", "ollama",
                        "ExecutionGateway", "DisabledGateway", "OrderPermission", "submit", "place_order"}
        for pkg in ("strategy", "risk"):
            for f in (APP / pkg).rglob("*.py"):
                tree = ast.parse(f.read_text())
                calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                         for n in ast.walk(tree) if isinstance(n, ast.Call)}
                names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                         for a in n.names} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                        {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
                self.assertEqual(calls & banned_calls, set(), f"{f.name}: clock/random/network call")
                self.assertEqual(names & banned_names, set(), f"{f.name}: forbidden dependency")
        self.assertIn("RiskSettings", (APP / "risk" / "config.py").read_text())

    def test_every_layer_package_has_purpose(self):
        for d in APP.iterdir():
            if d.is_dir() and not d.name.startswith("__"):
                init = d / "__init__.py"
                self.assertTrue(init.exists(), f"{d.name} missing __init__")
                self.assertTrue(ast.get_docstring(ast.parse(init.read_text())),
                                f"app/{d.name} has no docstring describing its responsibility")

    def test_no_old_project_imports(self):
        for f in APP.rglob("*.py"):
            for imp in imports_of(f):
                self.assertFalse(imp.startswith(("crypto_agent", "main", "bot")), f"{f}: {imp}")


class NetworkIsolationTests(unittest.TestCase):  # Test 10
    def test_guard_blocks_sockets_and_dns(self):
        import socket
        from tests import netguard
        with netguard.blocked():
            with self.assertRaises(netguard.NetworkBlocked):
                socket.create_connection(("api.bybit.com", 443), timeout=1)
            with self.assertRaises(netguard.NetworkBlocked):
                socket.getaddrinfo("api.bybit.com", 443)

    def test_real_transport_fails_loudly_when_offline(self):
        from app.data.errors import ConnectionFailed
        from app.data.http import UrllibTransport
        from tests import netguard
        with netguard.blocked(), self.assertRaises(ConnectionFailed):
            UrllibTransport().get("https://api.bybit.com/v5/market/time", {}, 2)

    def test_offline_baseline_active_under_runner(self):
        import os
        from tests import netguard
        if os.environ.get("QRE_TEST_RUNNER") != "1":
            self.skipTest("only meaningful when executed by test.py")
        self.assertTrue(netguard.ACTIVE, "test.py must run the baseline with the network guard on")

    def test_network_tests_are_opt_in(self):
        src = (APP.parent / "tests" / "test_md_network.py").read_text(encoding="utf-8")
        self.assertIn("QRE_NETWORK_TESTS", src)
