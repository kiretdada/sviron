@echo off
cd /d "%~dp0\..\.."
python tools\run_clone.py --clone "%~dp0"
