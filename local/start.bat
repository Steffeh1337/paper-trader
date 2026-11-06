@echo off
rem Trading Desk: pulls the latest paper-trader results, then opens the dashboard.
cd /d "%~dp0.."
git pull --quiet
py local\app.py --open
pause
