@echo off
rem Double-click in fight week: updates results, settles bets, takes an odds snapshot,
rem retrains if needed, builds the betting plan, then opens the dashboard.
cd /d "%~dp0"
python -m ufc_edge weekly
echo.
echo Opening the dashboard (close this window to stop it)...
python -m ufc_edge dashboard
pause
