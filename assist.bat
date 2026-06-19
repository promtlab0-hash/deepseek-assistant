@echo off
chcp 65001 >nul
title DeepSeek ассистент
REM Запускатель для Windows. Открой PowerShell/CMD в нужной папке и запусти этот .bat
REM (или дважды кликни). Работает из любой папки — скрипт берётся рядом с .bat.

set "SCRIPT=%~dp0assistant.py"

where py >nul 2>nul && (
    py "%SCRIPT%"
    goto done
)
where python >nul 2>nul && (
    python "%SCRIPT%"
    goto done
)
echo [!] Python не найден. Установи с https://python.org (галочка "Add Python to PATH").

:done
echo.
echo --- ассистент закрыт. Нажми любую клавишу. ---
pause >nul
