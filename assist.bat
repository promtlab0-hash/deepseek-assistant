@echo off
chcp 65001 >nul
title DeepSeek ассистент
REM Запуск ассистента на Windows. Дважды кликни или запусти из терминала.
set "PY=py"
where py >nul 2>nul || set "PY=python"
"%PY%" "%~dp0assistant.py"
echo.
echo --- ассистент закрылся. Нажми любую клавишу, чтобы выйти. ---
pause
