import io
import unittest

from app.config.settings import load_settings
from app.core.logging import get_logger, log_context, redact, setup_logging


class LoggingTests(unittest.TestCase):
    def test_levels_and_context(self):
        buf = io.StringIO()
        setup_logging("DEBUG", stream=buf)
        log = get_logger("test")
        with log_context(correlation_id="cor_1", order_id="ord_9"):
            log.debug("d"); log.info("i"); log.warning("w"); log.error("e")
        out = buf.getvalue()
        for lvl in ("DEBUG", "INFO", "WARNING", "ERROR"):
            self.assertIn(lvl, out)
        self.assertIn("correlation_id=cor_1 order_id=ord_9", out)
        with self.assertRaises(ValueError):
            with log_context(foo="x"):
                pass

    def test_secrets_redacted(self):
        load_settings(environ={"BYBIT_API_KEY": "Zk8sPq2LmN4x", "BYBIT_API_SECRET": "s3cr3tValue99"},
                      env_file=None)
        buf = io.StringIO()
        setup_logging("INFO", stream=buf)
        get_logger("t").info("request with key Zk8sPq2LmN4x and secret=s3cr3tValue99 api_key: abc123")
        out = buf.getvalue()
        self.assertNotIn("Zk8sPq2LmN4x", out)
        self.assertNotIn("s3cr3tValue99", out)
        self.assertNotIn("abc123", out)
        self.assertIn('"token": "***"', redact('{"token": "abcd1234"}'))
