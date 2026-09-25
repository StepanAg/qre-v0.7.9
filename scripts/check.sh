#!/usr/bin/env sh
# CI entry point: run the suite and keep a JSON report.
set -e
cd "$(dirname "$0")/.."
python test.py --json-file test-report.json
