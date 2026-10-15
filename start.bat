@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" "%~dp0install.py" start --dest "%~dp0."
start "" http://localhost:4999
timeout /t 3 >nul
