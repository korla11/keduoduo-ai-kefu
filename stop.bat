@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" "%~dp0install.py" stop --dest "%~dp0."
timeout /t 3 >nul
