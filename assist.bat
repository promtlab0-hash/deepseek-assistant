@echo off
title DeepSeek assistant
REM Launcher for Windows. Double-click or run from a terminal.
set "PY=py"
where py >nul 2>nul || set "PY=python"
"%PY%" "%~dp0assistant.py"
echo.
echo --- Assistant closed. Press any key to exit. ---
pause
