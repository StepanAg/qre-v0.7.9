@echo off
rem Double-click (or run in a terminal) to start the market monitor under the watchdog.
rem Examples:  run_monitor.bat              -> until Ctrl+C
rem            run_monitor.bat --hours 24   -> 24 hours
cd /d "%~dp0"
python -m app monitor supervise %*
echo.
pause
