#!/usr/bin/env sh
# Start the market monitor under the watchdog (Linux/macOS).
#   ./run_monitor.sh              -> until Ctrl+C
#   ./run_monitor.sh --hours 24   -> 24 hours
cd "$(dirname "$0")"
exec python3 -m app monitor supervise "$@"
