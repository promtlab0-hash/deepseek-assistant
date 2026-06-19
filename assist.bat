@echo off
REM Запускатель ассистента для Windows. Работает из любой папки:
REM открой PowerShell/CMD в нужной папке и запусти этот .bat.
python "%~dp0assistant.py" %*
