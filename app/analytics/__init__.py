"""Phase 8 analytics layer.

Measures what QRE has recorded - Phase 7 research runs and live monitor/setup data -
through a read-only source (storage/analytics_read.py, SQLite mode=ro). The two data
families are reported separately, sources QRE does not record are reported as
source_not_available, and nothing is ever written. The Phase 0 modules metrics.py and
domain/analytics.py are kept untouched for a future real-trade adapter."""
